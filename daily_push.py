# -*- coding: utf-8 -*-
"""
温湿度计监测 - 每日飞书推送脚本
================================
数据源：法拉IOT开放平台 API (open.xzfala.com)
推送目标：飞书群「深圳仓（物流服务部）」

功能：
1. 通过法拉开放API获取设备温湿度数据（API为主）
2. 生成飞书富文本（post）消息：按楼层区域展示
   - 区域名称、温湿度、电量、状态、联网 均加粗
   - 状态异常时红色加粗显示
   - 温湿度超出阈值时数值红色加粗，电量低时提醒充电
3. 设备编号 → 楼层区域精确映射，避免数据错位

用法：
    python daily_push.py            # 手动执行一次推送
    python daily_push.py --dry-run  # 只获取数据不推送，用于测试
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime

import requests

# ==================== 配置区 ====================
# 凭证读取顺序：
#   1. 环境变量（云端GitHub Actions通过 Secrets 注入）
#   2. 本地 config.json（被.gitignore排除，不会提交到仓库）
# 注意：严禁在脚本中硬编码真实凭证（GitHub 机密扫描会拦截）

def _load_config():
    """从本地 config.json 读取凭证（仅本地使用，不入库）"""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}

_config = _load_config()

FALA_USER_ID = os.environ.get("FALA_USER_ID", _config.get("FALA_USER_ID", ""))
FALA_USER_KEY = os.environ.get("FALA_USER_KEY", _config.get("FALA_USER_KEY", ""))
FALA_BASE_URL = "https://open.xzfala.com"

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", _config.get("FEISHU_APP_ID", ""))
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", _config.get("FEISHU_APP_SECRET", ""))
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", _config.get("FEISHU_CHAT_ID", ""))

_REQUIRED_CREDS = {
    "FALA_USER_ID": FALA_USER_ID,
    "FALA_USER_KEY": FALA_USER_KEY,
    "FEISHU_APP_ID": FEISHU_APP_ID,
    "FEISHU_APP_SECRET": FEISHU_APP_SECRET,
    "FEISHU_CHAT_ID": FEISHU_CHAT_ID,
}


def check_credentials():
    """检查凭证是否齐全，缺失则抛出异常"""
    missing = [k for k, v in _REQUIRED_CREDS.items() if not v]
    if missing:
        raise RuntimeError(
            "缺少凭证: " + ", ".join(missing)
            + "。请在GitHub仓库Settings→Secrets配置环境变量，"
            + "或在脚本同目录创建 config.json 提供凭证。"
        )

# 仓库名称
WAREHOUSE_NAME = "深圳物流仓"

# 设备编号 → 楼层区域名称（确保数据准确对应）
DEVICE_MAP = {
    "111112601290117": "深圳物流仓1F-货品周转区",
    "111112601290118": "深圳物流仓2F-跨境存储区",
    "111112601290121": "深圳物流仓3F-全棉存储区",
    "111112601290119": "深圳物流仓13F-香港存储区",
}

# 异常告警阈值（温湿度超出范围状态判为异常）
TEMP_MIN = 0.0    # 最低温度
TEMP_MAX = 30.0   # 最高温度
HUM_MIN = 45.0    # 最低湿度
HUM_MAX = 75.0    # 最高湿度
POWER_ALARM = 20  # 电量告警阈值（电量低于该值提示充电）
# ==================== 配置区结束 ====================


def fala_get_token(user_key, now_ms):
    """生成法拉API的token = MD5(key + 时间戳)"""
    return hashlib.md5(f"{user_key}{now_ms}".encode("utf-8")).hexdigest()


def fala_get_device_list():
    """调用 GET /device/list 获取设备基础信息"""
    now_ms = str(int(time.time() * 1000))
    headers = {
        "time": now_ms,
        "token": fala_get_token(FALA_USER_KEY, now_ms),
        "userId": FALA_USER_ID,
    }
    resp = requests.get(f"{FALA_BASE_URL}/device/list", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fala_get_device_info(device_nos=None):
    """调用 POST /device/info 获取设备实时数据（温度湿度等）

    device_nos: 设备编号列表，None则查询所有设备
    """
    now_ms = str(int(time.time() * 1000))
    headers = {
        "time": now_ms,
        "token": fala_get_token(FALA_USER_KEY, now_ms),
        "userId": FALA_USER_ID,
    }
    if device_nos:
        headers["deviceNo"] = ",".join(device_nos)
    resp = requests.post(f"{FALA_BASE_URL}/device/info", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _extract_sensor(info):
    """从实时数据中提取温度和湿度"""
    temp = hum = None
    for sensor in info.get("sensorList", []):
        stype = sensor.get("type")
        if stype == "temperature":
            temp = sensor.get("valueV2") or str(sensor.get("value", 0) / 10)
        elif stype == "humidity":
            hum = sensor.get("valueV2") or str(sensor.get("value", 0) / 10)
    return temp, hum


def parse_device_data(device_list, info_list):
    """合并设备列表和实时数据，按 DEVICE_MAP 顺序输出

    返回: [{device_no, region_name, temp, hum, power, signal, net_status}]
    """
    info_map = {i["deviceNo"]: i for i in info_list}
    base_map = {d["id"]: d for d in device_list}

    results = []
    # 按 DEVICE_MAP 顺序（1F, 2F, 3F, 13F）
    for dev_no in DEVICE_MAP:
        base = base_map.get(dev_no)
        info = info_map.get(dev_no, {})
        temp, hum = _extract_sensor(info)
        results.append({
            "device_no": dev_no,
            "region_name": DEVICE_MAP[dev_no],
            "temp": temp,
            "hum": hum,
            "power": info.get("power"),
            "signal": info.get("signal"),
            "net_status": base.get("netStatus", "unknown") if base else "unknown",
        })

    # 追加不在映射中的其他设备（若有）
    known = set(DEVICE_MAP.keys())
    for dev_no, base in base_map.items():
        if dev_no not in known:
            info = info_map.get(dev_no, {})
            temp, hum = _extract_sensor(info)
            results.append({
                "device_no": dev_no,
                "region_name": base.get("deviceName", dev_no),
                "temp": temp,
                "hum": hum,
                "power": info.get("power"),
                "signal": info.get("signal"),
                "net_status": base.get("netStatus", "unknown"),
            })
    return results


def judge_status(device):
    """根据阈值判断设备各项是否异常

    返回: (is_ok, detail)
      detail = {
        "temp_abnormal": bool,   # 温度是否超阈值
        "hum_abnormal": bool,    # 湿度是否超阈值
        "power_low": bool,       # 电量是否低于告警值
        "issues": [str],         # 异常描述列表
      }
    """
    detail = {"temp_abnormal": False, "hum_abnormal": False,
              "power_low": False, "issues": []}
    ok = True

    try:
        temp = float(device["temp"])
        if temp > TEMP_MAX:
            detail["temp_abnormal"] = True
            detail["issues"].append(f"温度偏高 {temp:g}℃（上限{TEMP_MAX:g}℃）")
            ok = False
        elif temp < TEMP_MIN:
            detail["temp_abnormal"] = True
            detail["issues"].append(f"温度偏低 {temp:g}℃（下限{TEMP_MIN:g}℃）")
            ok = False
    except (TypeError, ValueError):
        detail["temp_abnormal"] = True
        detail["issues"].append("温度数据缺失")
        ok = False

    try:
        hum = float(device["hum"])
        if hum > HUM_MAX:
            detail["hum_abnormal"] = True
            detail["issues"].append(f"湿度偏高 {hum:g}%（上限{HUM_MAX:g}%）")
            ok = False
        elif hum < HUM_MIN:
            detail["hum_abnormal"] = True
            detail["issues"].append(f"湿度偏低 {hum:g}%（下限{HUM_MIN:g}%）")
            ok = False
    except (TypeError, ValueError):
        detail["hum_abnormal"] = True
        detail["issues"].append("湿度数据缺失")
        ok = False

    # 电量告警
    try:
        power = float(device["power"])
        if power < POWER_ALARM:
            detail["power_low"] = True
            detail["issues"].append(f"电量偏低 {power:g}%（低于{POWER_ALARM:g}%）")
            ok = False
    except (TypeError, ValueError):
        pass  # 电量缺失不判异常

    return ok, detail


def build_post_content(devices):
    """生成飞书 post 富文本消息内容（支持加粗、红色字体）

    格式：
    仓库：深圳物流仓
    采集时间：2026-08-12 10:29
    ——————————————————
    深圳物流仓1F-货品周转区（加粗）
     🌡️ 温度：28.3℃（加粗，超阈值时红色）
    💧 湿度：60.6％（加粗，超阈值时红色）
    🔋 电量：41%（加粗）
    ✅ 状态：正常（加粗）/ ⚠️ 状态：异常（红色加粗）
    📶 联网：在线（加粗）
    ...
    ——————————————
    数据更新时间：2026-08-12 10:29
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = []

    def row(text, bold=False, red=False):
        style = []
        if bold:
            style.append("bold")
        if red:
            style.append("red")
        content.append([{"tag": "text", "text": text, "style": style}])

    # 标题区
    row(f"仓库：{WAREHOUSE_NAME}", bold=True)
    row(f"采集时间：{now_str}")
    row("——————————————————")

    # 各楼层区域
    for d in devices:
        temp = d["temp"] if d["temp"] is not None else "--"
        hum = d["hum"] if d["hum"] is not None else "--"
        power = d["power"] if d["power"] is not None else "--"
        net = "在线" if d["net_status"] == "online" else "离线"

        ok, detail = judge_status(d)

        # 区域名称加粗
        row(d["region_name"], bold=True)
        # 温度：异常时红色加粗
        row(f" 🌡️ 温度：{temp}℃", bold=True, red=detail["temp_abnormal"])
        # 湿度：异常时红色加粗
        row(f"💧 湿度：{hum}％", bold=True, red=detail["hum_abnormal"])
        # 电量：低于阈值时显示红色加粗充电提醒
        if detail["power_low"]:
            row(f"🔋 电量：{power}%", bold=True)
            row("⚠️ 请及时充电！", bold=True, red=True)
        else:
            row(f"🔋 电量：{power}%", bold=True)
        if ok:
            row("✅ 状态：正常", bold=True)
        else:
            row("⚠️ 状态：异常", bold=True, red=True)
            for issue in detail["issues"]:
                row(f"　└ {issue}", red=True)
        row(f"📶 联网：{net}", bold=True)
        row("")  # 空行分隔

    row("——————————————")
    row(f"数据更新时间：{now_str}")

    return {
        "zh_cn": {
            "title": "温湿度监测日报",
            "content": content,
        }
    }


# ==================== 飞书 API ====================

def feishu_get_token():
    """获取飞书 tenant_access_token"""
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书token失败: {data.get('msg')}")
    return data["tenant_access_token"]


def feishu_send_post(chat_id, post_content):
    """发送富文本（post）消息到群聊"""
    token = feishu_get_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={"receive_id": chat_id, "msg_type": "post",
              "content": json.dumps(post_content, ensure_ascii=False)},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"发送飞书消息失败: {data.get('msg')}")
    return data


def main():
    dry_run = "--dry-run" in sys.argv
    try:
        # 0. 检查凭证是否齐全
        check_credentials()

        # 1. 获取设备数据（API为主）
        device_list = fala_get_device_list()
        dev_nos = [d["id"] for d in device_list]
        info_list = fala_get_device_info(dev_nos)
        devices = parse_device_data(device_list, info_list)

        if not devices:
            print("⚠️ 未获取到任何设备数据")
            return 1

        post_content = build_post_content(devices)

        print("=== 设备数据 ===")
        print(json.dumps(devices, ensure_ascii=False, indent=2))
        print("\n=== 推送消息(post) ===")
        print(json.dumps(post_content, ensure_ascii=False, indent=2))

        if dry_run:
            print("\n(dry-run 模式，未发送消息)")
            return 0

        # 2. 发送到飞书（富文本）
        feishu_send_post(FEISHU_CHAT_ID, post_content)
        print("\n✅ 消息已推送到飞书群「深圳仓（物流服务部）」")

    except Exception as e:
        print(f"❌ 推送失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
