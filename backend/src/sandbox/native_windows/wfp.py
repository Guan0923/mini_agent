"""Persistent Windows Filtering Platform policy for fixed sandbox accounts.

The elevated installation helper is the only caller of this module.  The
Broker never mutates WFP state at run time: filters are keyed by the fixed
Offline/Online account identities and survive helper/service restarts.
"""

from __future__ import annotations

import ctypes
import os
import re
import uuid
from ctypes import wintypes
from dataclasses import dataclass

_RPC_C_AUTHN_WINNT = 10

_FWP_EMPTY = 0
_FWP_UINT8 = 1
_FWP_UINT16 = 2
_FWP_UINT32 = 3
_FWP_SECURITY_DESCRIPTOR_TYPE = 14

_FWP_MATCH_EQUAL = 0
_FWP_MATCH_FLAGS_ALL_SET = 6

_FWP_ACTION_BLOCK = 0x00001001
_FWP_ACTION_PERMIT = 0x00001002
_FWP_CONDITION_FLAG_IS_LOOPBACK = 0x00000001

_FWPM_PROVIDER_FLAG_PERSISTENT = 0x00000001
_FWPM_SUBLAYER_FLAG_PERSISTENT = 0x00000001
_FWPM_FILTER_FLAG_PERSISTENT = 0x00000001

_FWP_E_FILTER_NOT_FOUND = 0x80320003
_FWP_E_PROVIDER_NOT_FOUND = 0x80320005
_FWP_E_SUBLAYER_NOT_FOUND = 0x80320007

_PROVIDER_UUID = uuid.UUID("46f58f79-63f8-57c2-8c5c-5ac18fe1ed04")
_SUBLAYER_UUID = uuid.UUID("7c80efba-5207-521d-afb4-9ff8a25f9fa7")

_LAYER_CONNECT_V4 = uuid.UUID("c38d57d1-05a7-4c33-904f-7fbceee60e82")
_LAYER_CONNECT_V6 = uuid.UUID("4a72393b-319f-44bc-84c3-ba54dcb3b6b4")
_LAYER_RECV_ACCEPT_V4 = uuid.UUID("e1cd9fe7-f4b5-4273-96c0-592e487b8650")
_LAYER_RECV_ACCEPT_V6 = uuid.UUID("a3b42c97-9f04-4672-b87e-cee9c483257f")

_CONDITION_ALE_USER_ID = uuid.UUID("af043a0a-b34d-4f86-979c-c90371af6e66")
_CONDITION_FLAGS = uuid.UUID("632ce23b-5167-435c-86d7-e903684aa80c")
_CONDITION_IP_PROTOCOL = uuid.UUID("3971ef2b-623e-4f9a-8cb1-6e79b806b9a7")
_CONDITION_IP_REMOTE_PORT = uuid.UUID("c35a604d-d22b-4e1a-91b4-68f674ee674b")

_FILTER_NAMESPACE = uuid.UUID("71962528-c050-5993-a2cc-b15d3a71530f")
_SID_PATTERN = re.compile(r"S-\d+(?:-\d+)+", flags=re.IGNORECASE)


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> _Guid:
        raw = value.bytes_le
        return cls(
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:6], "little"),
            int.from_bytes(raw[6:8], "little"),
            (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:]),
        )


class _ByteBlob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _ValueUnion(ctypes.Union):
    _fields_ = [
        ("uint8", ctypes.c_uint8),
        ("uint16", ctypes.c_uint16),
        ("uint32", ctypes.c_uint32),
        ("pointer", ctypes.c_void_p),
    ]


class _Value(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_uint32), ("value", _ValueUnion)]


class _ConditionValueUnion(ctypes.Union):
    _fields_ = [
        ("uint8", ctypes.c_uint8),
        ("uint16", ctypes.c_uint16),
        ("uint32", ctypes.c_uint32),
        ("sd", ctypes.POINTER(_ByteBlob)),
        ("pointer", ctypes.c_void_p),
    ]


class _ConditionValue(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_uint32), ("value", _ConditionValueUnion)]


class _DisplayData(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("description", wintypes.LPWSTR)]


class _Provider(ctypes.Structure):
    _fields_ = [
        ("provider_key", _Guid),
        ("display_data", _DisplayData),
        ("flags", ctypes.c_uint32),
        ("provider_data", _ByteBlob),
        ("service_name", wintypes.LPWSTR),
    ]


class _SubLayer(ctypes.Structure):
    _fields_ = [
        ("sub_layer_key", _Guid),
        ("display_data", _DisplayData),
        ("flags", ctypes.c_uint32),
        ("provider_key", ctypes.POINTER(_Guid)),
        ("provider_data", _ByteBlob),
        ("weight", ctypes.c_uint16),
    ]


class _FilterCondition(ctypes.Structure):
    _fields_ = [
        ("field_key", _Guid),
        ("match_type", ctypes.c_uint32),
        ("condition_value", _ConditionValue),
    ]


class _ActionUnion(ctypes.Union):
    _fields_ = [("filter_type", _Guid), ("callout_key", _Guid), ("bitmap_index", ctypes.c_uint8)]


class _Action(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ctypes.c_uint32), ("value", _ActionUnion)]


class _ContextUnion(ctypes.Union):
    _fields_ = [("raw_context", ctypes.c_uint64), ("provider_context_key", _Guid)]


class _Filter(ctypes.Structure):
    _anonymous_ = ("context",)
    _fields_ = [
        ("filter_key", _Guid),
        ("display_data", _DisplayData),
        ("flags", ctypes.c_uint32),
        ("provider_key", ctypes.POINTER(_Guid)),
        ("provider_data", _ByteBlob),
        ("layer_key", _Guid),
        ("sub_layer_key", _Guid),
        ("weight", _Value),
        ("num_filter_conditions", ctypes.c_uint32),
        ("filter_condition", ctypes.POINTER(_FilterCondition)),
        ("action", _Action),
        ("context", _ContextUnion),
        ("reserved", ctypes.POINTER(_Guid)),
        ("filter_id", ctypes.c_uint64),
        ("effective_weight", _Value),
    ]


@dataclass(frozen=True, slots=True)
class StaticFilterSpec:
    name: str
    layer: uuid.UUID
    user_sid: str
    action: int
    weight: int
    loopback_only: bool = False
    tcp_only: bool = False
    remote_port: int | None = None

    @property
    def key(self) -> uuid.UUID:
        return uuid.uuid5(_FILTER_NAMESPACE, self.name)


def build_static_filter_specs(offline_sid: str, online_sid: str, proxy_port: int) -> tuple[StaticFilterSpec, ...]:
    """Build the complete persistent account policy without touching Windows."""

    _validate_inputs(offline_sid, online_sid, proxy_port)
    specs: list[StaticFilterSpec] = []
    for suffix, layer in (("v4", _LAYER_CONNECT_V4), ("v6", _LAYER_CONNECT_V6)):
        specs.extend(
            (
                StaticFilterSpec(
                    f"offline-connect-proxy-{suffix}",
                    layer,
                    offline_sid,
                    _FWP_ACTION_PERMIT,
                    15,
                    loopback_only=True,
                    tcp_only=True,
                    remote_port=proxy_port,
                ),
                StaticFilterSpec(f"offline-connect-block-{suffix}", layer, offline_sid, _FWP_ACTION_BLOCK, 0),
            )
        )
    for role, sid in (("offline", offline_sid), ("online", online_sid)):
        for suffix, layer in (("v4", _LAYER_RECV_ACCEPT_V4), ("v6", _LAYER_RECV_ACCEPT_V6)):
            specs.extend(
                (
                    StaticFilterSpec(
                        f"{role}-accept-loopback-{suffix}",
                        layer,
                        sid,
                        _FWP_ACTION_PERMIT,
                        15,
                        loopback_only=True,
                    ),
                    StaticFilterSpec(f"{role}-accept-block-{suffix}", layer, sid, _FWP_ACTION_BLOCK, 0),
                )
            )
    return tuple(specs)


def _validate_inputs(offline_sid: str, online_sid: str, proxy_port: int) -> None:
    if not _SID_PATTERN.fullmatch(offline_sid) or not _SID_PATTERN.fullmatch(online_sid):
        raise ValueError("Sandbox account SID is invalid")
    if offline_sid.casefold() == online_sid.casefold():
        raise ValueError("Sandbox account SIDs must be distinct")
    if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
        raise ValueError("Sandbox proxy port is invalid")


class _WfpApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Filtering Platform is unavailable")
        self.library = ctypes.WinDLL("fwpuclnt.dll", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32.dll", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self.library.FwpmEngineOpen0.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.library.FwpmEngineOpen0.restype = ctypes.c_uint32
        self.library.FwpmEngineClose0.argtypes = [wintypes.HANDLE]
        self.library.FwpmEngineClose0.restype = ctypes.c_uint32
        self.library.FwpmTransactionBegin0.argtypes = [wintypes.HANDLE, ctypes.c_uint32]
        self.library.FwpmTransactionBegin0.restype = ctypes.c_uint32
        self.library.FwpmTransactionCommit0.argtypes = [wintypes.HANDLE]
        self.library.FwpmTransactionCommit0.restype = ctypes.c_uint32
        self.library.FwpmTransactionAbort0.argtypes = [wintypes.HANDLE]
        self.library.FwpmTransactionAbort0.restype = ctypes.c_uint32
        self.library.FwpmProviderAdd0.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Provider), ctypes.c_void_p]
        self.library.FwpmProviderAdd0.restype = ctypes.c_uint32
        self.library.FwpmProviderDeleteByKey0.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Guid)]
        self.library.FwpmProviderDeleteByKey0.restype = ctypes.c_uint32
        self.library.FwpmSubLayerAdd0.argtypes = [wintypes.HANDLE, ctypes.POINTER(_SubLayer), ctypes.c_void_p]
        self.library.FwpmSubLayerAdd0.restype = ctypes.c_uint32
        self.library.FwpmSubLayerDeleteByKey0.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Guid)]
        self.library.FwpmSubLayerDeleteByKey0.restype = ctypes.c_uint32
        self.library.FwpmFilterAdd0.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Filter),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self.library.FwpmFilterAdd0.restype = ctypes.c_uint32
        self.library.FwpmFilterDeleteByKey0.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Guid)]
        self.library.FwpmFilterDeleteByKey0.restype = ctypes.c_uint32
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def check(code: int, operation: str, *, allowed: frozenset[int] = frozenset()) -> None:
        value = int(code) & 0xFFFFFFFF
        if value == 0 or value in allowed:
            return
        raise OSError(f"Windows Filtering Platform {operation} failed with 0x{value:08x}")

    def security_descriptor(self, sid: str) -> tuple[_ByteBlob, ctypes.Array[ctypes.c_ubyte]]:
        pointer = ctypes.c_void_p()
        size = ctypes.c_uint32()
        sddl = f"D:(A;;CC;;;{sid})"
        if not self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(pointer), ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            data = (ctypes.c_ubyte * size.value).from_buffer_copy(ctypes.string_at(pointer, size.value))
        finally:
            self.kernel.LocalFree(pointer)
        return _ByteBlob(size.value, ctypes.cast(data, ctypes.POINTER(ctypes.c_ubyte))), data


def configure_static_wfp(offline_sid: str, online_sid: str, proxy_port: int) -> None:
    """Atomically replace Mini-Agent's persistent WFP filters."""

    specs = build_static_filter_specs(offline_sid, online_sid, proxy_port)
    api = _WfpApi()
    handle = wintypes.HANDLE()
    api.check(api.library.FwpmEngineOpen0(None, _RPC_C_AUTHN_WINNT, None, None, ctypes.byref(handle)), "open")
    transaction_started = False
    try:
        api.check(api.library.FwpmTransactionBegin0(handle, 0), "transaction begin")
        transaction_started = True
        for spec in specs:
            key = _Guid.from_uuid(spec.key)
            api.check(
                api.library.FwpmFilterDeleteByKey0(handle, ctypes.byref(key)),
                "filter delete",
                allowed=frozenset({_FWP_E_FILTER_NOT_FOUND}),
            )
        sublayer_key = _Guid.from_uuid(_SUBLAYER_UUID)
        api.check(
            api.library.FwpmSubLayerDeleteByKey0(handle, ctypes.byref(sublayer_key)),
            "sublayer delete",
            allowed=frozenset({_FWP_E_SUBLAYER_NOT_FOUND}),
        )
        provider_key = _Guid.from_uuid(_PROVIDER_UUID)
        api.check(
            api.library.FwpmProviderDeleteByKey0(handle, ctypes.byref(provider_key)),
            "provider delete",
            allowed=frozenset({_FWP_E_PROVIDER_NOT_FOUND}),
        )

        provider = _Provider(
            provider_key,
            _DisplayData("Mini-Agent run_command sandbox", "Static filters for fixed sandbox accounts"),
            _FWPM_PROVIDER_FLAG_PERSISTENT,
            _ByteBlob(),
            None,
        )
        api.check(api.library.FwpmProviderAdd0(handle, ctypes.byref(provider), None), "provider add")
        sublayer = _SubLayer(
            sublayer_key,
            _DisplayData("Mini-Agent run_command sandbox", "Account network isolation"),
            _FWPM_SUBLAYER_FLAG_PERSISTENT,
            ctypes.pointer(provider_key),
            _ByteBlob(),
            0x7000,
        )
        api.check(api.library.FwpmSubLayerAdd0(handle, ctypes.byref(sublayer), None), "sublayer add")
        for spec in specs:
            _add_filter(api, handle, provider_key, sublayer_key, spec)
        api.check(api.library.FwpmTransactionCommit0(handle), "transaction commit")
        transaction_started = False
    except Exception:
        if transaction_started:
            api.library.FwpmTransactionAbort0(handle)
        raise
    finally:
        api.library.FwpmEngineClose0(handle)


def remove_static_wfp() -> None:
    """Atomically remove only Mini-Agent's persistent WFP objects."""

    api = _WfpApi()
    handle = wintypes.HANDLE()
    api.check(api.library.FwpmEngineOpen0(None, _RPC_C_AUTHN_WINNT, None, None, ctypes.byref(handle)), "open")
    transaction_started = False
    try:
        api.check(api.library.FwpmTransactionBegin0(handle, 0), "transaction begin")
        transaction_started = True
        for spec in build_static_filter_specs("S-1-5-21-1-1-1-1", "S-1-5-21-1-1-1-2", 1):
            key = _Guid.from_uuid(spec.key)
            api.check(
                api.library.FwpmFilterDeleteByKey0(handle, ctypes.byref(key)),
                "filter delete",
                allowed=frozenset({_FWP_E_FILTER_NOT_FOUND}),
            )
        sublayer_key = _Guid.from_uuid(_SUBLAYER_UUID)
        api.check(
            api.library.FwpmSubLayerDeleteByKey0(handle, ctypes.byref(sublayer_key)),
            "sublayer delete",
            allowed=frozenset({_FWP_E_SUBLAYER_NOT_FOUND}),
        )
        provider_key = _Guid.from_uuid(_PROVIDER_UUID)
        api.check(
            api.library.FwpmProviderDeleteByKey0(handle, ctypes.byref(provider_key)),
            "provider delete",
            allowed=frozenset({_FWP_E_PROVIDER_NOT_FOUND}),
        )
        api.check(api.library.FwpmTransactionCommit0(handle), "transaction commit")
        transaction_started = False
    except Exception:
        if transaction_started:
            api.library.FwpmTransactionAbort0(handle)
        raise
    finally:
        api.library.FwpmEngineClose0(handle)


def _add_filter(
    api: _WfpApi,
    handle: wintypes.HANDLE,
    provider_key: _Guid,
    sublayer_key: _Guid,
    spec: StaticFilterSpec,
) -> None:
    sd_blob, sd_buffer = api.security_descriptor(spec.user_sid)
    conditions: list[_FilterCondition] = []
    user_value = _ConditionValue()
    user_value.type = _FWP_SECURITY_DESCRIPTOR_TYPE
    user_value.sd = ctypes.pointer(sd_blob)
    conditions.append(_FilterCondition(_Guid.from_uuid(_CONDITION_ALE_USER_ID), _FWP_MATCH_EQUAL, user_value))
    if spec.loopback_only:
        value = _ConditionValue()
        value.type = _FWP_UINT32
        value.uint32 = _FWP_CONDITION_FLAG_IS_LOOPBACK
        conditions.append(_FilterCondition(_Guid.from_uuid(_CONDITION_FLAGS), _FWP_MATCH_FLAGS_ALL_SET, value))
    if spec.tcp_only:
        value = _ConditionValue()
        value.type = _FWP_UINT8
        value.uint8 = 6
        conditions.append(_FilterCondition(_Guid.from_uuid(_CONDITION_IP_PROTOCOL), _FWP_MATCH_EQUAL, value))
    if spec.remote_port is not None:
        value = _ConditionValue()
        value.type = _FWP_UINT16
        value.uint16 = spec.remote_port
        conditions.append(_FilterCondition(_Guid.from_uuid(_CONDITION_IP_REMOTE_PORT), _FWP_MATCH_EQUAL, value))
    condition_array = (_FilterCondition * len(conditions))(*conditions)
    weight = _Value()
    weight.type = _FWP_UINT8
    weight.uint8 = spec.weight
    action = _Action()
    action.type = spec.action
    filter_value = _Filter(
        _Guid.from_uuid(spec.key),
        _DisplayData(f"Mini-Agent {spec.name}", "Fixed-account run_command isolation"),
        _FWPM_FILTER_FLAG_PERSISTENT,
        ctypes.pointer(provider_key),
        _ByteBlob(),
        _Guid.from_uuid(spec.layer),
        sublayer_key,
        weight,
        len(condition_array),
        ctypes.cast(condition_array, ctypes.POINTER(_FilterCondition)),
        action,
        _ContextUnion(),
        None,
        0,
        _Value(_FWP_EMPTY, _ValueUnion()),
    )
    filter_id = ctypes.c_uint64()
    api.check(
        api.library.FwpmFilterAdd0(handle, ctypes.byref(filter_value), None, ctypes.byref(filter_id)), "filter add"
    )
    # Keep all pointed-to objects alive until FwpmFilterAdd0 has copied them.
    _ = (sd_buffer, sd_blob, condition_array)


__all__ = ["StaticFilterSpec", "build_static_filter_specs", "configure_static_wfp", "remove_static_wfp"]
