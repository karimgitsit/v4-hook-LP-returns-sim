"""Deploy v4 hooks at flag-valid addresses (Python HookMiner + CREATE2 factory).

v4 encodes a hook's permissions in the low 14 bits of its address, and
`PoolManager.initialize` rejects a non-zero hook whose address has zero flag
bits. To deploy an arbitrary hook at an address with the required bits we:

1. deploy a tiny CREATE2 factory (so deployment is address-deterministic),
2. brute-force a salt in Python until
   ``CREATE2(factory, salt, initcode) & 0x3FFF == flags``,
3. call the factory with ``salt ++ initcode`` to deploy the hook (running its
   constructor, so this works for stateful hooks too).

This mirrors v4-periphery's HookMiner.find.
"""

from __future__ import annotations

from eth_utils import keccak

from .env import DEFAULT_GAS_LIMIT, V4Env

# Low 14 address bits carry hook permissions (v4-core Hooks.sol).
ALL_HOOK_MASK = (1 << 14) - 1  # 0x3FFF

# Permission flag bit positions (value = 1 << bit).
BEFORE_INITIALIZE_FLAG = 1 << 13
AFTER_INITIALIZE_FLAG = 1 << 12
BEFORE_ADD_LIQUIDITY_FLAG = 1 << 11
AFTER_ADD_LIQUIDITY_FLAG = 1 << 10
BEFORE_REMOVE_LIQUIDITY_FLAG = 1 << 9
AFTER_REMOVE_LIQUIDITY_FLAG = 1 << 8
BEFORE_SWAP_FLAG = 1 << 7
AFTER_SWAP_FLAG = 1 << 6
BEFORE_DONATE_FLAG = 1 << 5
AFTER_DONATE_FLAG = 1 << 4
BEFORE_SWAP_RETURNS_DELTA_FLAG = 1 << 3
AFTER_SWAP_RETURNS_DELTA_FLAG = 1 << 2
AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG = 1 << 1
AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG = 1 << 0

# Minimal CREATE2 factory. Calldata = [32-byte salt][initcode...]; it CREATE2s
# the initcode with that salt and returns the 32-byte-padded new address.
# Runtime (29 bytes), assembled from opcodes:
#   PUSH1 0x20 CALLDATASIZE SUB        ; size = calldatasize - 32
#   DUP1 PUSH1 0x20 PUSH1 0x00 CALLDATACOPY   ; mem[0:size] = initcode
#   PUSH1 0x00 CALLDATALOAD SWAP1      ; load salt, reorder
#   PUSH1 0x00 PUSH1 0x00 CREATE2      ; addr = create2(0, 0, size, salt)
#   PUSH1 0x00 MSTORE PUSH1 0x20 PUSH1 0x00 RETURN
_FACTORY_RUNTIME = bytes(
    [
        0x60, 0x20, 0x36, 0x03, 0x80, 0x60, 0x20, 0x60, 0x00, 0x37,
        0x60, 0x00, 0x35, 0x90, 0x60, 0x00, 0x60, 0x00, 0xF5, 0x60,
        0x00, 0x52, 0x60, 0x20, 0x60, 0x00, 0xF3,
    ]
)
# Deploy wrapper that returns the runtime above (offset 0x0b, length 0x1b=27).
_FACTORY_INITCODE = (
    bytes([0x60, len(_FACTORY_RUNTIME), 0x80, 0x60, 0x0B, 0x60, 0x00, 0x39, 0x60, 0x00, 0xF3])
    + _FACTORY_RUNTIME
)

_MAX_SALT_ITERS = 2_000_000


def deploy_create2_factory(env: V4Env) -> str:
    """Deploy the CREATE2 factory and return its address."""
    addr = env.evm.deploy(env.deployer, _FACTORY_INITCODE, gas=DEFAULT_GAS_LIMIT)
    if not addr:
        raise RuntimeError("CREATE2 factory deploy returned empty address")
    return addr


def compute_create2_address(factory: str, salt: bytes, init_code: bytes) -> str:
    """Standard CREATE2 address: keccak(0xff ++ factory ++ salt ++ keccak(initcode))[12:]."""
    if len(salt) != 32:
        raise ValueError("salt must be 32 bytes")
    factory_bytes = bytes.fromhex(factory[2:] if factory.startswith("0x") else factory)
    digest = keccak(b"\xff" + factory_bytes + salt + keccak(init_code))
    return "0x" + digest[12:].hex()


def mine_hook_salt(
    factory: str, init_code: bytes, flags: int, *, flag_mask: int = ALL_HOOK_MASK
) -> tuple[bytes, str]:
    """Find a salt so the CREATE2 address has ``addr & flag_mask == flags``."""
    if flags & ~flag_mask:
        raise ValueError(f"flags {flags:#x} set bits outside mask {flag_mask:#x}")
    for i in range(_MAX_SALT_ITERS):
        salt = i.to_bytes(32, "big")
        addr = compute_create2_address(factory, salt, init_code)
        if int(addr, 16) & flag_mask == flags:
            return salt, addr
    raise RuntimeError(f"no salt found for flags {flags:#x} within {_MAX_SALT_ITERS} iters")


def deploy_hook(
    env: V4Env,
    creation_code: bytes,
    flags: int,
    *,
    constructor_args: bytes = b"",
    factory: str | None = None,
) -> str:
    """Mine a flag-valid address and CREATE2-deploy the hook there.

    `creation_code` is the artifact's creation bytecode; `constructor_args` are
    ABI-encoded args appended to it. Returns the deployed hook address.
    """
    factory = factory or deploy_create2_factory(env)
    init_code = creation_code + constructor_args
    salt, expected = mine_hook_salt(factory, init_code, flags)
    out = env.evm.message_call(
        caller=env.deployer,
        to=factory,
        calldata=salt + init_code,
        gas=DEFAULT_GAS_LIMIT,
    )
    returned = out if isinstance(out, (bytes, bytearray)) else bytes(out)
    addr = "0x" + returned[-20:].hex()
    if int(addr, 16) != int(expected, 16):
        raise RuntimeError(f"deployed address {addr} != mined {expected}")
    if not env.evm.get_code(addr):
        raise RuntimeError(f"no code at deployed hook address {addr}")
    return addr
