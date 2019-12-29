#!/usr/local/bin/python
# -*- coding: utf-8 -*-

import json
import time
import os
import signal
import subprocess
import sys

from datetime import datetime
from pathlib import Path

try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote

import riprova

from bitcoin.rpc import InWarmupError, Proxy
from prometheus_client import start_http_server, Gauge, Counter


# Create Prometheus metrics to track bitcoind stats.
BITCOIN_BLOCKS = Gauge('bitcoin_blocks', 'Block height')
BITCOIN_DIFFICULTY = Gauge('bitcoin_difficulty', 'Difficulty')
BITCOIN_PEERS = Gauge('bitcoin_peers', 'Number of peers')
BITCOIN_HASHPS_NEG1 = Gauge('bitcoin_hashps_neg1', 'Estimated network hash rate per second since the last difficulty change')
BITCOIN_HASHPS_1 = Gauge('bitcoin_hashps_1', 'Estimated network hash rate per second for the last block')
BITCOIN_HASHPS = Gauge('bitcoin_hashps', 'Estimated network hash rate per second for the last 120 blocks')

BITCOIN_ESTIMATED_SMART_FEE_GAUGES = {}

BITCOIN_WARNINGS = Counter('bitcoin_warnings', 'Number of network or blockchain warnings detected')
BITCOIN_UPTIME = Gauge('bitcoin_uptime', 'Number of seconds the Bitcoin daemon has been running')

BITCOIN_MEMINFO_USED = Gauge('bitcoin_meminfo_used', 'Number of bytes used')
BITCOIN_MEMINFO_FREE = Gauge('bitcoin_meminfo_free', 'Number of bytes available')
BITCOIN_MEMINFO_TOTAL = Gauge('bitcoin_meminfo_total', 'Number of bytes managed')
BITCOIN_MEMINFO_LOCKED = Gauge('bitcoin_meminfo_locked', 'Number of bytes locked')
BITCOIN_MEMINFO_CHUNKS_USED = Gauge('bitcoin_meminfo_chunks_used', 'Number of allocated chunks')
BITCOIN_MEMINFO_CHUNKS_FREE = Gauge('bitcoin_meminfo_chunks_free', 'Number of unused chunks')

BITCOIN_MEMPOOL_BYTES = Gauge('bitcoin_mempool_bytes', 'Size of mempool in bytes')
BITCOIN_MEMPOOL_SIZE = Gauge('bitcoin_mempool_size', 'Number of unconfirmed transactions in mempool')
BITCOIN_MEMPOOL_USAGE = Gauge('bitcoin_mempool_usage', 'Total memory usage for the mempool')

BITCOIN_LATEST_BLOCK_HEIGHT = Gauge('bitcoin_latest_block_height', 'Height or index of latest block')
BITCOIN_LATEST_BLOCK_WEIGHT = Gauge('bitcoin_latest_block_weight', 'Weight of latest block according to BIP 141')
BITCOIN_LATEST_BLOCK_SIZE = Gauge('bitcoin_latest_block_size', 'Size of latest block in bytes')
BITCOIN_LATEST_BLOCK_TXS = Gauge('bitcoin_latest_block_txs', 'Number of transactions in latest block')

BITCOIN_NUM_CHAINTIPS = Gauge('bitcoin_num_chaintips', 'Number of known blockchain branches')

BITCOIN_TOTAL_BYTES_RECV = Gauge('bitcoin_total_bytes_recv', 'Total bytes received')
BITCOIN_TOTAL_BYTES_SENT = Gauge('bitcoin_total_bytes_sent', 'Total bytes sent')

BITCOIN_LATEST_BLOCK_INPUTS = Gauge('bitcoin_latest_block_inputs', 'Number of inputs in transactions of latest block')
BITCOIN_LATEST_BLOCK_OUTPUTS = Gauge('bitcoin_latest_block_outputs', 'Number of outputs in transactions of latest block')
BITCOIN_LATEST_BLOCK_VALUE = Gauge('bitcoin_latest_block_value', 'Bitcoin value of all transactions in the latest block')

BITCOIN_BAN_CREATED = Gauge('bitcoin_ban_created', 'Time the ban was created', labelnames=['address', 'reason'])
BITCOIN_BANNED_UNTIL = Gauge('bitcoin_banned_until', 'Time the ban expires', labelnames=['address', 'reason'])

BITCOIN_SERVER_VERSION = Gauge('bitcoin_server_version', 'The server version')
BITCOIN_PROTOCOL_VERSION = Gauge('bitcoin_protocol_version', 'The protocol version of the server')

BITCOIN_SIZE_ON_DISK = Gauge('bitcoin_size_on_disk', 'Estimated size of the block and undo files')

EXPORTER_ERRORS = Counter('bitcoin_exporter_errors', 'Number of errors encountered by the exporter', labelnames=['type'])
PROCESS_TIME = Counter('bitcoin_exporter_process_time', 'Time spent processing metrics from bitcoin node')


BITCOIN_RPC_SCHEME = os.environ.get('BITCOIN_RPC_SCHEME', 'http')
BITCOIN_RPC_HOST = os.environ.get('BITCOIN_RPC_HOST', 'localhost')
BITCOIN_RPC_PORT = os.environ.get('BITCOIN_RPC_PORT', '8332')
BITCOIN_RPC_USER = os.environ.get('BITCOIN_RPC_USER')
BITCOIN_RPC_PASSWORD = os.environ.get('BITCOIN_RPC_PASSWORD')
SMART_FEES = [int(f) for f in os.environ.get('SMARTFEE_BLOCKS', "2,3,5,20").split(",")]
REFRESH_SECONDS = float(os.environ.get('REFRESH_SECONDS', '300'))
METRICS_PORT = int(os.environ.get('METRICS_PORT', '8334'))
RETRIES = int(os.environ.get('RETRIES', 5))
TIMEOUT = int(os.environ.get('TIMEOUT', 30))


RETRY_EXCEPTIONS = (
    InWarmupError,
    ConnectionRefusedError,
)


def on_retry(err, next_try):
    err_type = type(err)
    exception_name = err_type.__module__ + "." + err_type.__name__
    EXPORTER_ERRORS.labels(**{"type": exception_name}).inc()


def error_evaluator(e):
    return isinstance(e, RETRY_EXCEPTIONS)


@riprova.retry(
    timeout=TIMEOUT,
    backoff=riprova.ExponentialBackOff(),
    on_retry=on_retry,
    error_evaluator=error_evaluator,
)
def bitcoinrpc(*args):
    bitcoin_conf = Path.home() / ".bitcoin" / "bitcoin.conf"
    if bitcoin_conf.exists():
        proxy = Proxy(btc_conf_file=bitcoin_conf)
    else:
        host = BITCOIN_RPC_HOST
        if BITCOIN_RPC_USER and BITCOIN_RPC_PASSWORD:
            host = "%s:%s@%s" % (
                quote(BITCOIN_RPC_USER),
                quote(BITCOIN_RPC_PASSWORD),
                host,
            )
        if BITCOIN_RPC_PORT:
            host = "%s:%s" % (host, BITCOIN_RPC_PORT)
        service_url = "%s://%s" % (BITCOIN_RPC_SCHEME, host)
        proxy = Proxy(service_url=service_url)
    result = proxy.call(*args)
    return result


def get_block(block_hash):
    try:
        block = bitcoinrpc('getblock', block_hash, 2)
    except Exception as e:
        print(e)
        print('Error: Can\'t retrieve block ' + block_hash + ' from bitcoind.')
        return None
    return block


def smartfee_gauge(num_blocks):
    gauge = BITCOIN_ESTIMATED_SMART_FEE_GAUGES.get(num_blocks)
    if gauge is None:
        gauge = Gauge(
            'bitcoin_est_smart_fee_%d' % num_blocks,
            'Estimated smart fee per kilobyte for confirmation in %d blocks' % num_blocks
        )
        BITCOIN_ESTIMATED_SMART_FEE_GAUGES[num_blocks] = gauge
    return gauge


def do_smartfee(num_blocks):
    smartfee = bitcoinrpc('estimatesmartfee', num_blocks).get('feerate')
    if smartfee is not None:
        gauge = smartfee_gauge(num_blocks)
        gauge.set(smartfee)


def refresh_metrics():
    uptime = int(bitcoinrpc('uptime'))
    meminfo = bitcoinrpc('getmemoryinfo', 'stats')['locked']
    blockchaininfo = bitcoinrpc('getblockchaininfo')
    networkinfo = bitcoinrpc('getnetworkinfo')
    chaintips = len(bitcoinrpc('getchaintips'))
    mempool = bitcoinrpc('getmempoolinfo')
    nettotals = bitcoinrpc('getnettotals')
    latest_block = get_block(str(blockchaininfo['bestblockhash']))
    hashps_120 = float(bitcoinrpc('getnetworkhashps', 120))  # 120 is the default
    hashps_neg1 = float(bitcoinrpc('getnetworkhashps', -1))
    hashps_1 = float(bitcoinrpc('getnetworkhashps', 1))

    banned = bitcoinrpc('listbanned')

    BITCOIN_UPTIME.set(uptime)
    BITCOIN_BLOCKS.set(blockchaininfo['blocks'])
    BITCOIN_PEERS.set(networkinfo['connections'])
    BITCOIN_DIFFICULTY.set(blockchaininfo['difficulty'])
    BITCOIN_HASHPS.set(hashps_120)
    BITCOIN_HASHPS_NEG1.set(hashps_neg1)
    BITCOIN_HASHPS_1.set(hashps_1)
    BITCOIN_SERVER_VERSION.set(networkinfo['version'])
    BITCOIN_PROTOCOL_VERSION.set(networkinfo['protocolversion'])
    BITCOIN_SIZE_ON_DISK.set(blockchaininfo['size_on_disk'])

    for smartfee in SMART_FEES:
        do_smartfee(smartfee)

    for ban in banned:
        BITCOIN_BAN_CREATED.labels(address=ban['address'], reason=ban['ban_reason']).set(ban['ban_created'])
        BITCOIN_BANNED_UNTIL.labels(address=ban['address'], reason=ban['ban_reason']).set(ban['banned_until'])

    if networkinfo['warnings']:
        BITCOIN_WARNINGS.inc()

    BITCOIN_NUM_CHAINTIPS.set(chaintips)

    BITCOIN_MEMINFO_USED.set(meminfo['used'])
    BITCOIN_MEMINFO_FREE.set(meminfo['free'])
    BITCOIN_MEMINFO_TOTAL.set(meminfo['total'])
    BITCOIN_MEMINFO_LOCKED.set(meminfo['locked'])
    BITCOIN_MEMINFO_CHUNKS_USED.set(meminfo['chunks_used'])
    BITCOIN_MEMINFO_CHUNKS_FREE.set(meminfo['chunks_free'])

    BITCOIN_MEMPOOL_BYTES.set(mempool['bytes'])
    BITCOIN_MEMPOOL_SIZE.set(mempool['size'])
    BITCOIN_MEMPOOL_USAGE.set(mempool['usage'])

    BITCOIN_TOTAL_BYTES_RECV.set(nettotals['totalbytesrecv'])
    BITCOIN_TOTAL_BYTES_SENT.set(nettotals['totalbytessent'])

    if latest_block is not None:
        BITCOIN_LATEST_BLOCK_SIZE.set(latest_block['size'])
        BITCOIN_LATEST_BLOCK_TXS.set(latest_block['nTx'])
        BITCOIN_LATEST_BLOCK_HEIGHT.set(latest_block['height'])
        BITCOIN_LATEST_BLOCK_WEIGHT.set(latest_block['weight'])
        inputs, outputs = 0, 0
        value = 0
        for tx in latest_block['tx']:
            i = len(tx['vin'])
            inputs += i
            o = len(tx['vout'])
            outputs += o
            value += sum(o["value"] for o in tx['vout'])

        BITCOIN_LATEST_BLOCK_INPUTS.set(inputs)
        BITCOIN_LATEST_BLOCK_OUTPUTS.set(outputs)
        BITCOIN_LATEST_BLOCK_VALUE.set(value)


def sigterm_handler(signal, frame):
    print('Received SIGTERM. Exiting.')
    sys.exit(0)


def exception_count(e):
    err_type = type(e)
    exception_name = err_type.__module__ + "." + err_type.__name__
    EXPORTER_ERRORS.labels(**{"type": exception_name}).inc()


def main():
    signal.signal(signal.SIGTERM, sigterm_handler)

    # Start up the server to expose the metrics.
    start_http_server(METRICS_PORT)
    while True:
        process_start = datetime.now()

        # Allow riprova.MaxRetriesExceeded and unknown exceptions to crash the process.
        try:
            refresh_metrics()
        except riprova.exceptions.RetryError as e:
            print("Refresh failed during retry. Cause: " + str(e))
            exception_count(e)
        except json.decoder.JSONDecodeError as e:
            print("RPC call did not return JSON. Bad credentials? " + str(e))
            sys.exit(1)

        duration = datetime.now() - process_start
        PROCESS_TIME.inc(duration.total_seconds())

        time.sleep(REFRESH_SECONDS)


if __name__ == '__main__':
    main()
