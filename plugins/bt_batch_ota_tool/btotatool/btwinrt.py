# -*- coding: utf-8 -*-
"""
WinRT 蓝牙设备 / RFCOMM 服务查询。

用途：告诉用户「这台设备到底支持哪些蓝牙 SPP 服务」，
以及给 Windows 的串口服务授权（request_access_async）。

依赖 `winrt` 包（Python/WinRT 投影，pip install winrt）。
没装也不影响主功能（BLE / OTA），只是 SPP 那块信息显示不出来。
"""

from __future__ import annotations

import re
import sys
from typing import List, Optional, Tuple

#: 本工程关心的 SPP 服务（key 必须全小写 —— 实际 uuid 是小写形式）
SPP_UUIDS = {
    "00001101-0000-1000-8000-00805f9b34fb": "串口(Serial Port)",
    "00006666-0000-1000-8000-00805f9b34fb": "OTA(自定义)",
    "0000111e-0000-1000-8000-00805f9b34fb": "音频接收(A2DP Sink)",
    "0000110a-0000-1000-8000-00805f9b34fb": "音频源(A2DP Source)",
    "0000110e-0000-1000-8000-00805f9b34fb": "AVRCP",
}

_UUID_RE = re.compile(r"\{([0-9a-fA-F-]{36})\}")


#: 本进程内已经确认支持 SPP 的设备：{mac去分隔符: [(uuid, 名称), ...]}
#:
#: 为什么需要：设备临时不可达时，SDP 查询会返回空列表（不报错），
#: 如果不记住"这台明明支持 SPP"，就会误报成"固件没开放串口"。
#: 一旦查到过就以已知结果为准，避免状态来回跳。
_KNOWN = {}


def _known_path() -> str:
    """已确认支持 SPP 的设备缓存文件（放 exe 同级或用户目录）。"""
    import os
    base = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "spp_known.json")


def _load_known() -> None:
    """从磁盘载入已确认的设备表。"""
    if _KNOWN:
        return
    import json
    import os
    try:
        with open(_known_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list) and v:
                    _KNOWN[k] = [(str(u), str(n)) for u, n in v]
    except Exception:
        pass


def _save_known() -> None:
    """把已确认的设备表写回磁盘。"""
    import json
    import os
    try:
        with open(_known_path(), "w", encoding="utf-8") as f:
            json.dump({k: [list(x) for x in v] for k, v in _KNOWN.items()}, f,
                      ensure_ascii=False, indent=1)
    except Exception:
        pass


def _remember(mac: str, services) -> None:
    key = mac.replace(":", "").lower()
    if services:
        _KNOWN[key] = list(services)
        _save_known()


def _dev_name(bd) -> str:
    """取 BluetoothDevice 的 name（失败返回空串）。"""
    try:
        return bd.name or ""
    except Exception:
        return ""


def _is_placeholder_name(name: str) -> bool:
    """
    判断设备名是不是 Windows 生成的**占位名**。

    Windows 对"只留下配对记录、实体从未被真正访问过"的设备，
    会把 name 填成 `Bluetooth 50:c0:f0:86:ee:a1` 这种格式。
    真正访问过的设备才有真名（如 `XFN_50c0f0b7a566`）。

    这是判断"SDP 查询结果是否可信"的关键：
    占位名设备即使 from_id_async 不返回 None，SDP 查询也只是返回空壳。
    """
    n = (name or "").strip().lower()
    if not n:
        return True
    if n.startswith("bluetooth "):
        return True
    # 形如 "Bluetooth 50:c0:f0:86:ee:a1" / "50:c0:f0:86:ee:a1"
    if re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", n):
        return True
    return False


def available() -> bool:
    try:
        import winrt.windows.devices.bluetooth  # noqa: F401
        return True
    except ImportError:
        return False


def _run(coro):
    """
    跑一个协程并返回结果。

    必须用全新的事件循环：WinRT 的异步操作在复用旧 loop 时会静默失败
    （表现为 find_all_async 抛异常 / from_id_async 返回 None）。

    若当前线程已经有一个在跑的 loop（asyncio.run 会直接报错），
    就另起一个线程跑独立 loop —— WinRT 的调用是同步等待结果，
    这样最省事也最稳。
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # 当前线程已有事件循环 → 另起线程
    import threading

    box = {}

    def _worker():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_worker)
    th.start()
    th.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


_DEVICE_IDS = {}

#: 本机蓝牙 radio 的 MAC（device id 里的前缀部分），例如 "c0:09:25:79:3c:df"。
#: 有了它就能在 WinRT 枚举不到某设备时手工拼出 device id 兜底。
_RADIO_PREFIX = ""


def _load_device_ids(force: bool = False) -> dict:
    """
    一次性枚举系统里的蓝牙设备，返回 {mac去分隔符: device_id}。

    必须缓存：WinRT 的 find_all_async 是全量枚举，
    每台设备都调一次的话，5 台设备就是 5 次全量扫，容易超时/失败
    （而且失败会被当成"设备不支持 SPP"，误报）。

    WinRT 的 get_device_selector() 只返回**当前在线可访问**的设备，
    注册表里"已配对但离线"的设备不会出现在这里。
    """
    global _RADIO_PREFIX
    if _DEVICE_IDS and not force:
        return _DEVICE_IDS
    from winrt.windows.devices.bluetooth import BluetoothDevice
    from winrt.windows.devices.enumeration import DeviceInformation

    async def _go():
        sel = BluetoothDevice.get_device_selector()
        try:
            devs = await DeviceInformation.find_all_async(sel, [])
        except TypeError:
            devs = await DeviceInformation.find_all_async(sel)
        out = {}
        for d in devs:
            did = getattr(d, "id", "") or ""
            m = re.search(r"-([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})$", did)
            if not m:
                continue
            out[m.group(1).replace(":", "").lower()] = did
        return out

    try:
        ids = _run(_go())
    except Exception:
        return _DEVICE_IDS
    if ids:
        _DEVICE_IDS.clear()
        _DEVICE_IDS.update(ids)
        # 记录 radio 前缀（device id 形如 Bluetooth#Bluetooth<radio_mac>-<dev_mac>）
        if not _RADIO_PREFIX:
            first = next(iter(ids.values()))
            m = re.match(r"(Bluetooth#Bluetooth[0-9a-fA-F:]{17})-", first)
            if m:
                _RADIO_PREFIX = m.group(1)
    return _DEVICE_IDS


def _device_id(mac: str) -> Optional[str]:
    """通过蓝牙地址反查系统里的 device id（带缓存 + 手工拼接兜底）。"""
    key = mac.replace(":", "").lower()
    ids = _load_device_ids()
    if key in ids:
        return ids[key]
    # 缓存里没有 → 重新全量枚举一次再找
    ids = _load_device_ids(force=True)
    if key in ids:
        return ids[key]
    # 还是没有：WinRT 只列在线设备，离线的设备枚举不到。
    # 这时用已知的 radio 前缀手工拼 id —— 实测对离线设备同样有效。
    return _synth_device_id(mac)


def _synth_device_id(mac: str) -> Optional[str]:
    """
    用本机 radio 前缀 + 目标 MAC 手工拼一个 device id。

    WinRT 的 get_device_selector() 只返回在线设备，但 SDP 查询用
    手工拼的 id 依然能拿到服务表（实测有效）。
    """
    if not _RADIO_PREFIX:
        # 还没拿到 radio 前缀 → 先枚举一次去取
        _load_device_ids(force=True)
    if not _RADIO_PREFIX:
        return None
    mac_flat = mac.replace(":", "").lower()
    if len(mac_flat) != 12:
        return None
    mac_colon = ":".join(mac_flat[i:i + 2] for i in range(0, 12, 2))
    return "%s-%s" % (_RADIO_PREFIX, mac_colon)


def scan_nearby_bt(timeout: float = 12.0) -> List[Tuple[str, str]]:
    """
    用 WinRT 做**经典蓝牙 inquiry**，发现附近「可发现」的蓝牙设备
    （含经典 BR/EDR 设备，如耳机、音箱、手机本身的经典服务——
     这些不广播 BLE，纯 BLE 扫描扫不到，所以手机上能看到、工具却扫不到）。

    返回 [(mac去分隔符小写, 设备名), ...]。超时或 winrt 不可用时返回空列表。

    原理：`DeviceInformation.find_all_async(BluetoothDevice.get_device_selector())`
    在 Windows 上会发起 inquiry 并返回可发现设备（Windows「添加设备」向导同款）。
    注意 inquiry 较慢（系统约 10s 量级），调用方应放在独立线程并行跑。
    """
    try:
        from winrt.windows.devices.bluetooth import BluetoothDevice
        from winrt.windows.devices.enumeration import DeviceInformation
    except ImportError:
        return []

    import threading
    import asyncio

    box: dict = {}

    def _worker():
        async def _go():
            sel = BluetoothDevice.get_device_selector()
            try:
                devs = await DeviceInformation.find_all_async(sel, [])
            except TypeError:
                devs = await DeviceInformation.find_all_async(sel)
            out = []
            for d in devs:
                did = getattr(d, "id", "") or ""
                name = getattr(d, "name", "") or ""
                m = re.search(r"-([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})$", did)
                if not m:
                    continue
                mac = m.group(1).replace(":", "").lower()
                out.append((mac, name))
            return out

        try:
            box["v"] = asyncio.run(_go())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        # 超时 → 返回已收集到的（可能为空），不让扫描整体卡死
        return list(box.get("v", []))
    return list(box.get("v", []))


def list_rfcomm_services(mac: str, retry: int = 1, debug=None) -> List[Tuple[str, str]]:
    """
    列出设备提供的 RFCOMM 服务，返回 [(服务UUID小写, 名称), ...]。

    查不到返回空列表（设备没配对 / 不支持 / winrt 没装 / 设备离线）。
    retry：首次失败后再重试一次 —— SDP 查询偶尔会因为设备刚好在重连而读不到。
    debug：可选回调，用来把失败原因打出来（别再静默吞）。
    """
    services, _err = query_rfcomm_services(mac, retry=retry, debug=debug)
    return services


def query_rfcomm_services(mac: str, retry: int = 1, debug=None):
    """
    查询设备的 RFCOMM 服务，同时返回失败原因。

    返回 (services, err)：
      services —— [(服务UUID小写, 名称), ...]
      err     —— 空串表示查询成功（哪怕服务列表为空），
                 非空表示"没能拿到结论"（设备离线 / WinRT 打不开 / 异常）。
                 **这个区别很重要**：err 为空但 services 也空 = 固件真没提供串口服务；
                 err 非空 = 只是查不到，不能断言固件不支持。
    """
    from winrt.windows.devices.bluetooth import BluetoothDevice

    _load_known()
    did = _device_id(mac)
    if not did:
        if debug:
            debug("找不到 %s 的系统 device id（设备可能离线，或 WinRT 枚举失败）" % mac)
        cached = _KNOWN.get(mac.replace(":", "").lower())
        if cached:
            return (list(cached), "")
        return ([], "找不到设备 id")

    async def _go():
        bd = await BluetoothDevice.from_id_async(did)
        if bd is None:
            return None
        res = await bd.get_rfcomm_services_async()
        svcs = list(getattr(res, "services", None) or [])
        out = []
        seen = set()
        for s in svcs:
            name = s.connection_service_name or ""
            m = _UUID_RE.search(name)
            if not m:
                continue
            uuid = m.group(1).lower()
            # 同一个 UUID 可能出现多次（不同 RFCOMM channel，如 00000000 / 00000001），
            # 按 UUID 去重，避免界面上出现两个"串口"。
            if uuid in seen:
                continue
            seen.add(uuid)
            label = SPP_UUIDS.get(uuid, "")
            out.append((uuid, label or uuid[:8]))
        return out, _dev_name(bd)

    last_err = ""
    for attempt in range(retry + 1):
        try:
            r = _run(_go())
            if r is None:
                last_err = "设备打不开（离线或未配对）"
                if attempt < retry:
                    import time
                    time.sleep(0.8)
                    continue
                return ([], last_err)
            out, devname = r
            if out:
                _remember(mac, out)
                return (out, "")
            # 服务列表为空 —— 要分清"固件真没开放串口"和"只是设备不可达"。
            # 判据一：Windows 只有真实设备名（不是"Bluetooth <MAC>" 这种占位名）
            #         才说明这台设备曾经被真正访问过，SDP 查询结果可信。
            if _is_placeholder_name(devname):
                last_err = "设备当前不可达（Windows 只有占位记录，未真正访问过）"
                if attempt < retry:
                    import time
                    time.sleep(0.8)
                    continue
                return ([], last_err)
            # 判据二：本进程内已经查到过这台设备支持 SPP，
            #         那一次空结果多半是设备临时不可达，不该改口。
            cached = _KNOWN.get(mac.replace(":", "").lower())
            if cached:
                _remember(mac, cached)
                return (list(cached), "")
            # 查询成功，服务列表为空 —— 固件确实没提供这些服务
            return ([], "")
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        if attempt < retry:
            import time
            time.sleep(0.8)
    if debug:
        debug("%s 查询 SPP 服务失败（%s）" % (mac, last_err))
    # 最后兜底：曾经查到过就仍然按已知结果报
    cached = _KNOWN.get(mac.replace(":", "").lower())
    if cached:
        return (list(cached), "")
    return ([], last_err)


#: 链路状态判定结果
LINK_OFFLINE = "offline"      # 设备不可达（打不开 / 只有占位记录）
LINK_DISCONNECTED = "disconnected"  # 设备可达，但当前没有活动连接
LINK_CONNECTED = "connected"  # 当前有活动连接
LINK_UNPAIRED = "unpaired"    # 设备可达，但**尚未配对**


def query_pairing_state(mac: str) -> Tuple[bool, bool]:
    """
    查询设备的配对状态。

    返回 (is_paired, can_pair)：
      is_paired —— 是否已配对。**未配对时 SPP 直连必然失败**，
                   这是最容易被误判成"设备不在范围内"的真实原因。
      can_pair  —— 系统认为还能不能配对（已配对的一般为 False）。
    """
    from winrt.windows.devices.enumeration import DeviceInformation

    did = _device_id(mac)
    if not did:
        return (False, False)

    async def _go():
        di = await DeviceInformation.create_from_id_async(did)
        if di is None:
            return None
        p = di.pairing
        return (bool(p.is_paired), bool(p.can_pair))

    try:
        r = _run(_go())
    except Exception:
        return (False, False)
    if r is None:
        return (False, False)
    return r


def show_pair_dialog(mac: str, timeout: float = 60.0) -> Tuple[str, str]:
    """
    **主动弹出 Windows 系统的蓝牙配对对话框**（"添加设备"）。

    用的是 WinRT 的 ``DeviceInformationCustomPairing.pair_async``：
    它会调起系统自带的配对 UI（就是你在右下角看到的那个"添加设备"），
    用户点确认即可完成配对。

    **多轮尝试**：DEFAULT(0) 失败后会降级到 NONE(1)（对齐安卓 insecure
    RFCOMM 的配对级别），共 3 轮、间隔递增 —— 应对设备刚被连接过、
    ACL 没清干净、息屏没响应等"第一次总失败"的场景。

    返回 (结果, 说明)：
      "paired"   —— 配对成功
      "cancelled"—— 用户取消 / 超时
      "failed"   —— 失败（说明里带原因）

    注意 ``pair_async`` 的返回值是 **DevicePairingResult 对象**（不是枚举！），
    真正的状态在它的 ``.status`` 属性上。
    """
    from winrt.windows.devices.enumeration import DeviceInformation

    did = _device_id(mac)
    if not did:
        return ("failed", "找不到设备的系统 id")

    # ---- 多轮尝试 + 保护级降级 + 自动应答 ----
    # 实测（手表设备）：
    #   · DEFAULT(0) 保护级立刻 19(FAILED) —— 认证谈判谈不出来；
    #   · NONE(1) 保护级返回 7(AUTHENTICATION_TIMEOUT) —— 设备其实在响应，
    #     只是确认仪式没人应答（息屏时设备也没法展示 passkey）。
    # 所以：① 先用 NONE(1)（对齐安卓 createInsecureRfcommSocket 的配对级别）；
    #       ② 注册 pairing_requested 处理器自动应答确认仪式，
    #          Just Works(CONFIRM_ONLY) 场景用户连确认都不用点。
    attempts = [
        (1, 0.0),   # 第 1 轮：NONE（对齐安卓 insecure，配自动应答）
        (0, 2.0),   # 第 2 轮：DEFAULT，系统自决
        (1, 4.0),   # 第 3 轮：再给一次机会（等用户唤醒设备）
    ]
    # 这些状态码值得再试：设备忙/没醒/上一条链路没清干净
    RETRYABLE = (19, 1, 2, 7)  # FAILED / NOT_READY_TO_PAIR / NOT_PAIRED / AUTH_TIMEOUT

    # pairing_requested 自动应答：确认仪式（Just Works）直接 accept，
    # 要求 PIN 的仪式自动填 0000（很多嵌入式设备的默认 PIN）。
    # 没有这个处理器，CONFIRM_ONLY 仪式会一直等到超时（状态码 7）。
    def _on_pairing_requested(sender, args):
        try:
            kind = int(getattr(args, "pairing_kind", 0) or 0)
        except Exception:
            kind = 0
        try:
            if kind & 0x4:          # PROVIDE_PIN —— 对方要我们输 PIN
                args.accept("0000")
            else:                   # CONFIRM_ONLY / CONFIRM_PIN_MATCH / DISPLAY_PIN
                args.accept()
        except Exception:
            pass

    async def _pair_once(di, level: int):
        """跑一轮配对，返回 (code, errmsg)；code=None 表示本地调用就失败了。"""
        token = None
        try:
            token = di.pairing.custom.add_pairing_requested(_on_pairing_requested)
        except Exception:
            token = None            # 投影不支持事件订阅就退回旧行为
        try:
            try:
                op = di.pairing.custom.pair_async(level)
            except Exception as e:
                return (None, "调起配对界面失败：%s" % e)
            res = await op
        finally:
            if token is not None:
                try:
                    di.pairing.custom.remove_pairing_requested(token)
                except Exception:
                    pass

        # ---- 解析结果：res 是 DevicePairingResult 对象，状态在 .status ----
        # 注意：pair_async 返回的**不是枚举**，而是 DevicePairingResult 对象，
        # 状态在 .status 属性上（类型 DevicePairingResultStatus）。
        # 之前直接 int(res) 会抛异常 → 一律退化成 -1「结果码 -1」，
        # 配对到底成功没成功根本看不出来。
        status = getattr(res, "status", None)
        if status is None:
            status = res  # 极端情况：老版本 winrt 直接返回枚举
        code = None
        if status is not None:
            try:
                code = int(status)
            except Exception:
                try:
                    code = int(status.value)
                except Exception:
                    code = None
        if code is None:
            return (None, "无法解析配对结果（%r）" % (res,))
        return (code, "")

    async def _go():
        import asyncio
        di = await DeviceInformation.create_from_id_async(did)
        if di is None:
            return ("failed", "打不开设备（离线或未配对）")
        pairing = di.pairing
        if pairing.is_paired:
            return ("paired", "设备已配对，无需再配对")
        if not pairing.can_pair:
            return ("failed", "系统认为该设备已不可配对")

        # DevicePairingResultStatus 枚举值：
        #   0=PAIRED 1=NOT_READY_TO_PAIR 2=NOT_PAIRED 3=ALREADY_PAIRED
        #   4=CONNECTION_REJECTED 5=TOO_MANY_CONNECTIONS 6=HARDWARE_FAILURE
        #   7=AUTHENTICATION_TIMEOUT 8=AUTHENTICATION_NOT_ALLOWED
        #   9=AUTHENTICATION_FAILURE 10=NO_SUPPORTED_PROFILES
        #   11=PROTECTION_LEVEL_COULD_NOT_BE_MET 12=ACCESS_DENIED
        #   13=INVALID_CEREMONY_DATA 14=PAIRING_CANCELED
        #   15=OPERATION_ALREADY_IN_PROGRESS 16=REQUIRED_HANDLER_NOT_REGISTERED
        #   17=REJECTED_BY_HANDLER 18=REMOTE_DEVICE_HAS_ASSOCIATION 19=FAILED
        names = {
            1: "设备当前未准备好配对", 2: "配对未完成", 4: "连接被拒绝",
            5: "连接数过多", 6: "硬件故障", 7: "认证超时",
            8: "系统不允许认证", 9: "认证失败", 10: "没有共同支持的配对协议",
            11: "无法满足保护级别（设备可能要求 PIN）", 12: "访问被拒绝",
            13: "配对数据无效", 16: "没有注册的配对处理器",
            17: "被配对处理器拒绝", 19: "设备拒绝或未响应配对请求",
        }

        last_code, last_err = None, ""
        for level, delay in attempts:
            if delay:
                await asyncio.sleep(delay)
            # 每轮开跑前先复查真实配对状态 —— 结果码不可全信，
            # 上一轮其实配好了但状态码骗人的情况实测出现过。
            try:
                di_chk = await DeviceInformation.create_from_id_async(did)
                if di_chk is not None and di_chk.pairing.is_paired:
                    return ("paired", "配对成功")
            except Exception:
                pass

            code, err = await _pair_once(di, level)
            if code is None:
                return ("failed", err)   # 本地调用就挂了，重试没用
            last_code = code
            if code in (0, 3):           # PAIRED / ALREADY_PAIRED
                return ("paired", "配对成功")
            if code == 14:               # PAIRING_CANCELED —— 用户主动取消，别再重试
                return ("cancelled", "用户取消了配对")
            if code == 18:               # REMOTE_DEVICE_HAS_ASSOCIATION —— 重试没用
                return ("failed", "设备已有蓝牙关联，请先在系统蓝牙设置里断开它")
            # **结果码不可全信** —— 失败码不代表真失败，复查一次真实状态
            try:
                di2 = await DeviceInformation.create_from_id_async(did)
                if di2 is not None and di2.pairing.is_paired:
                    return ("paired", "配对成功")
            except Exception:
                pass
            if code == 15:               # OPERATION_ALREADY_IN_PROGRESS —— 等等再试
                last_err = "配对正在进行中"
                continue
            if code not in RETRYABLE:
                last_err = ""
                break                    # 这类失败重试也没用（PIN/占用/被拒）
            last_err = ""

        # ---- 全部轮次跑完仍失败 ----
        reason = names.get(last_code, last_err or "未知原因") if last_code is not None \
            else (last_err or "未知原因")
        # 补一条"下一步怎么做"，别让用户对着干瞪眼
        lv_txt = ""
        if last_code in (7, 19):
            lv_txt = "（设备可能息屏无法响应/确认配对 —— 点亮它的屏幕、靠近电脑再试）"
        elif last_code == 11:
            lv_txt = "（设备可能要求 PIN 码，请在设备屏幕上查看并手动输入）"
        elif last_code == 12:
            lv_txt = "（请确认没有其他程序占用蓝牙适配器）"
        # 文案**不带前缀**（GUI 统一加"配对未完成："，避免前缀嵌套两次）
        return ("failed", "配对失败（状态码 %s）%s%s" % (
            last_code if last_code is not None else "?", reason, lv_txt))

    try:
        return _run(_go())
    except Exception as e:
        return ("failed", "%s: %s" % (type(e).__name__, e))


def query_link_state(mac: str) -> Tuple[str, str]:
    """
    查询设备的蓝牙链路状态。

    返回 (状态, 说明)：
      "connected"    —— 设备可达且当前有活动连接
      "disconnected" —— 设备可达，但当前**没有**活动连接
      "unpaired"     —— 设备可达，但**还没配对**（SPP 直连必然失败，
                        必须先在系统里"添加设备"完成配对）
      "offline"      —— 设备不可达（打不开 / Windows 里只有占位记录）

    为什么要单独查：Windows 上「已配对」≠「已连接」。
    SPP 直连要求设备在线可访问，如果设备没连上，
    应该明确告诉用户「先去系统蓝牙设置里连上」，
    而不是笼统地说「连不上」。

    判据（实测）：
      * BluetoothDevice.from_id_async 返回 None → offline
      * 设备名是 "Bluetooth <MAC>" 这种占位名 → 从未真正访问过 → offline
      * DeviceInformation.pairing.is_paired == False → unpaired
        （实测 a566 就是这种：Windows 右下角弹"添加设备"提示的真正原因）
      * connection_status == CONNECTED → connected
      * 其余（能打开、能取到真实设备名，但没活动连接）→ disconnected
    """
    from winrt.windows.devices.bluetooth import BluetoothDevice

    did = _device_id(mac)
    if not did:
        return (LINK_OFFLINE, "找不到设备的系统记录（可能未配对）")

    async def _go():
        bd = await BluetoothDevice.from_id_async(did)
        if bd is None:
            return None
        return bd

    try:
        bd = _run(_go())
    except Exception as e:
        return (LINK_OFFLINE, "%s: %s" % (type(e).__name__, e))
    if bd is None:
        return (LINK_OFFLINE, "设备当前不可达（未上电或不在范围内）")

    name = _dev_name(bd)
    if _is_placeholder_name(name):
        return (LINK_OFFLINE, "Windows 里只有占位记录，未真正访问过这台设备")

    # 配对状态最关键：没配对时 SPP 直连必然失败
    try:
        is_paired, can_pair = query_pairing_state(mac)
        if not is_paired and can_pair:
            return (LINK_UNPAIRED, "设备尚未配对 —— 需要先在系统里添加/配对")
        if not is_paired and not can_pair:
            return (LINK_UNPAIRED, "设备未配对，且系统当前不允许配对")
    except Exception:
        pass

    try:
        from winrt.windows.devices.bluetooth import BluetoothConnectionStatus
        cs = bd.connection_status
        if cs == BluetoothConnectionStatus.CONNECTED:
            return (LINK_CONNECTED, "已连接")
    except Exception:
        pass
    return (LINK_DISCONNECTED, "设备可达，但当前没有活动连接")


def request_service_access(mac: str, uuid: str) -> Tuple[bool, str]:
    """
    请求访问设备的某个 RFCOMM 服务（相当于给串口服务授权）。

    返回 (是否允许, 说明)。允许后 Windows 才有条件为它建立串口。
    """
    import asyncio

    from winrt.windows.devices.bluetooth import BluetoothDevice

    did = _device_id(mac)
    if not did:
        return (False, "找不到设备的系统 id")

    async def _go():
        bd = await BluetoothDevice.from_id_async(did)
        if bd is None:
            return (False, "打不开设备")
        res = await bd.get_rfcomm_services_async()
        svcs = list(getattr(res, "services", None) or [])
        target = uuid.lower().strip("{}")
        for s in svcs:
            name = s.connection_service_name or ""
            m = _UUID_RE.search(name)
            if not m or m.group(1).lower() != target:
                continue
            st = await s.request_access_async()
            code = int(st) if hasattr(st, "value") else int(st)
            return (code == 1, {0: "未知", 1: "允许", 2: "被用户拒绝", 3: "被系统拒绝"}.get(code, str(code)))
        return (False, "设备没有该服务")

    try:
        return _run(_go())
    except Exception as e:
        return (False, str(e))
