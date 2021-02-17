import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from functools import partial, wraps
from itertools import zip_longest
from pathlib import Path

import toml
from brownie import MerkleDistributor, Wei, accounts, interface, rpc, web3
from eth_abi import decode_single, encode_single
from eth_abi.packed import encode_abi_packed
from eth_utils import encode_hex
from toolz import valfilter, valmap
from tqdm import tqdm, trange
from click import secho

DISTRIBUTION_AMOUNT = Wei('8000000 ether')
DISTRIBUTOR_ADDRESS = '0x5e37996bcfF8C169e77b00D7b6e7261bbC60761e'
START_BLOCK = 10950650
SNAPSHOT_BLOCK = 10954410
TOKENS = {
    'EMN': '0x5ade7ae8660293f2ebfcefaba91d141d72d221e8',
    'eCRV': '0xb387e90367f1e621e656900ed2a762dc7d71da8c',
    'eLINK': '0xe4ffd682380c571a6a07dd8f20b402412e02830e',
    'eAAVE': '0xc08f38f43adb64d16fe9f9efcc2949d9eddec198',
    'eYFI': '0xed35197cadf01fcbfe6cfc11081f299cffb095bf',
    'eSNX': '0xd77c2ab1cd0faa4b79e16a0e7472cb222a9ee175',
}

TOKENS = valmap(interface.EminenceCurrency, TOKENS)
EMN = TOKENS['EMN']
DAI = interface.ERC20('0x6B175474E89094C44Da98b954EedeAC495271d0F')
UNISWAP_FACTORY = '0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f'
ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'


def cached(path):
    path = Path(path)
    codec = {'.toml': toml, '.json': json}[path.suffix]
    codec_args = {'.json': {'indent': 2}}.get(path.suffix, {})

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if path.exists():
                print('load from cache', path)
                return codec.loads(path.read_text())
            else:
                result = func(*args, **kwargs)
                os.makedirs(path.parent, exist_ok=True)
                path.write_text(codec.dumps(result, **codec_args))
                print('write to cache', path)
                return result

        return wrapper

    return decorator


def transfers_to_balances(address):
    balances = Counter()
    contract = web3.eth.contract(address, abi=DAI.abi)
    for start in trange(START_BLOCK, SNAPSHOT_BLOCK, 1000):
        end = min(start + 999, SNAPSHOT_BLOCK)
        logs = contract.events.Transfer().getLogs(fromBlock=start, toBlock=end)
        for log in logs:
            if log['args']['src'] != ZERO_ADDRESS:
                balances[log['args']['src']] -= log['args']['wad']
            if log['args']['dst'] != ZERO_ADDRESS:
                balances[log['args']['dst']] += log['args']['wad']

    return valfilter(bool, dict(balances.most_common()))


@cached('snapshot/01-balances.toml')
def step_01():
    print('step 01. snapshot token balances.')
    balances = defaultdict(Counter)  # token -> user -> balance
    for name, address in TOKENS.items():
        print(f'processing {name}')
        balances[name] = transfers_to_balances(str(address))
        assert min(balances[name].values()) >= 0, 'negative balances found'

    return balances


def ensure_archive_node():
    fresh = web3.eth.call(
        {'to': str(EMN), 'data': EMN.totalSupply.encode_input()})
    old = web3.eth.call(
        {'to': str(EMN), 'data': EMN.totalSupply.encode_input()}, SNAPSHOT_BLOCK)
    assert fresh != old, 'this step requires an archive node'


def convert_to_dai(name, balance):
    token = TOKENS[name]
    tx = {'to': str(
        token), 'data': EMN.calculateContinuousBurnReturn.encode_input(balance)}
    dai = decode_single('uint', web3.eth.call(tx, SNAPSHOT_BLOCK))
    if name in {'eCRV', 'eLINK', 'eAAVE', 'eYFI', 'eSNX'}:
        tx = {
            'to': str(EMN), 'data': EMN.calculateContinuousBurnReturn.encode_input(dai)}
        dai = decode_single('uint', web3.eth.call(tx, SNAPSHOT_BLOCK))
    return dai


@cached('snapshot/02-burn-to-dai.toml')
def step_02(balances):
    print('step 02. normalize balances to dai.')
    ensure_archive_node()

    dai_balances = Counter()  # user -> dai equivalent
    for name in balances:
        print(f'processing {name}')
        pool = ThreadPoolExecutor(10)
        futures = pool.map(partial(convert_to_dai, name),
                           balances[name].values())
        for user, dai in tqdm(zip(balances[name], futures), total=len(balances[name])):
            dai_balances[user] += dai

    dai_balances = valfilter(bool, dict(dai_balances.most_common()))
    dai_total = Wei(sum(dai_balances.values())).to('ether')
    print(f'equivalent value: {dai_total:,.0f} DAI')

    return dai_balances


@cached('snapshot/03-contracts.toml')
def step_03(balances):
    print('step 03. contract addresses.')
    pool = ThreadPoolExecutor(10)
    codes = pool.map(web3.eth.getCode, balances)
    contracts = {user: balances[user] for user, code in tqdm(
        zip(balances, codes), total=len(balances)) if code}
    print(f'{len(contracts)} contracts found')
    return contracts


def is_uniswap(address):
    try:
        pair = interface.UniswapPair(address)
        assert pair.factory() == UNISWAP_FACTORY
        print(f'{address} is a uniswap pool')
    except (AssertionError, ValueError):
        return False
    return True


def is_balancer(address):
    try:
        pair = interface.BalancerPool(address)
        assert pair.getColor() == encode_hex(encode_single('bytes32', b'BRONZE'))
        print(f'{address} is a balancer pool')
    except (AssertionError, ValueError):
        return False
    return True


@cached('snapshot/04-uniswap-balancer-lp.toml')
def step_04(contracts):
    print('step 04. uniswap and balancer lp.')
    replacements = {}
    for address in contracts:
        if not (is_uniswap(address) or is_balancer(address)):
            continue

        # no need to check the pool contents since we already know the equivalent dai value
        # so we just grab the lp share distribution and distirbute the dai pro-rata

        balances = transfers_to_balances(address)
        supply = sum(balances.values())
        if not supply:
            continue
        replacements[address] = {user: int(
            Fraction(balances[user], supply) * contracts[address]) for user in balances}
        assert sum(replacements[address].values()
                   ) <= contracts[address], 'no inflation ser'

    return replacements


@cached('snapshot/05-dai-with-lp.toml')
def step_05(balances, replacements):
    print('step 05. replace liquidity pools with their distributions.')
    for remove, additions in replacements.items():
        balances.pop(remove)
        for user, balance in additions.items():
            balances.setdefault(user, 0)
            balances[user] += balance
    return dict(Counter(balances).most_common())


@cached('snapshot/06-dai-pro-rata.toml')
def step_06(balances):
    print('step 06. pro-rata distribution')
    total = sum(balances.values())
    balances = valfilter(lambda value: value >= Wei('1 ether'), balances)
    pro_rata = {user: int(Fraction(balance, total) * DISTRIBUTION_AMOUNT)
                for user, balance in balances.items()}
    assert sum(pro_rata.values()
               ) <= DISTRIBUTION_AMOUNT, 'extravagant expenses ser'
    return pro_rata


class MerkleTree:
    def __init__(self, elements):
        self.elements = sorted(set(web3.keccak(hexstr=el) for el in elements))
        self.layers = MerkleTree.get_layers(self.elements)

    @property
    def root(self):
        return self.layers[-1][0]

    def get_proof(self, el):
        el = web3.keccak(hexstr=el)
        idx = self.elements.index(el)
        proof = []
        for layer in self.layers:
            pair_idx = idx + 1 if idx % 2 == 0 else idx - 1
            if pair_idx < len(layer):
                proof.append(encode_hex(layer[pair_idx]))
            idx //= 2
        return proof

    @staticmethod
    def get_layers(elements):
        layers = [elements]
        while len(layers[-1]) > 1:
            layers.append(MerkleTree.get_next_layer(layers[-1]))
        return layers

    @staticmethod
    def get_next_layer(elements):
        return [MerkleTree.combined_hash(a, b) for a, b in zip_longest(elements[::2], elements[1::2])]

    @staticmethod
    def combined_hash(a, b):
        if a is None:
            return b
        if b is None:
            return a
        return web3.keccak(b''.join(sorted([a, b])))


@cached('snapshot/07-merkle-distribution.json')
def step_07(balances):
    elements = [(index, account, amount)
                for index, (account, amount) in enumerate(balances.items())]
    nodes = [encode_hex(encode_abi_packed(
        ['uint', 'address', 'uint'], el)) for el in elements]
    tree = MerkleTree(nodes)
    distribution = {
        'merkleRoot': encode_hex(tree.root),
        'tokenTotal': hex(sum(balances.values())),
        'claims': {
            user: {'index': index, 'amount': hex(
                amount), 'proof': tree.get_proof(nodes[index])}
            for index, user, amount in elements
        },
    }
    print(f'merkle root: {encode_hex(tree.root)}')
    return distribution


def deploy():
    user = accounts[0] if rpc.is_active(
    ) else accounts.load(input('account: '))
    tree = json.load(open('snapshot/07-merkle-distribution.json'))
    root = tree['merkleRoot']
    token = str(DAI)
    MerkleDistributor.deploy(token, root, {'from': user})


def claim():
    claimer = accounts.load(input('Enter brownie account: '))
    dist = MerkleDistributor.at(DISTRIBUTOR_ADDRESS)
    tree = json.load(open('snapshot/07-merkle-distribution.json'))
    claim_other = input('Claim for another account? y/n [default: n] ') or 'n'
    assert claim_other in {'y', 'n'}
    user = str(claimer) if claim_other == 'n' else input(
        'Enter address to claim for: ')

    if user not in tree['claims']:
        return secho(f'{user} is not included in the distribution', fg='red')
    claim = tree['claims'][user]
    if dist.isClaimed(claim['index']):
        return secho(f'{user} has already claimed', fg='yellow')

    amount = Wei(int(claim['amount'], 16)).to('ether')
    secho(f'Claimable amount: {amount} DAI', fg='green')
    if claim_other == 'n':  # no tipping for others
        secho(
            '\nThe return of funds to you was made possible by a team of volunteers who worked for free to make this happen.'
            '\nPlease consider tipping them a portion of your recovered funds as a way to say thank you.\n',
            fg='yellow',
        )
        tip = input('Enter tip amount in percent: ')
        tip = int(float(tip.rstrip('%')) * 100)
        assert 0 <= tip <= 10000, 'invalid tip amount'
    else:
        tip = 0

    tx = dist.claim(claim['index'], user, claim['amount'],
                    claim['proof'], tip, {'from': claimer})
    tx.info()


def main():
    token_balances = step_01()
    dai_balances = step_02(token_balances)
    contracts = step_03(dai_balances)
    replacements = step_04(contracts)
    dai_balances = step_05(dai_balances, replacements)
    dai_pro_rata = step_06(dai_balances)
    step_07(dai_pro_rata)
