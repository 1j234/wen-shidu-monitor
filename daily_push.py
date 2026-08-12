# -*- coding: utf-8 -*-
"""
温湿度计监测 - 每日飞书推送脚本
================================
数据源：法拉IOT开放平台 API (open.xzfala.com)
推送目标：飞书群「深圳仓（物流服务部）」
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
def _load_config():
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
    missing = [k for k, v in _REQUIRED_CREDS.items() if not v]
    if missing:
        raise RuntimeError(
            "缺少凭证: " + ", ".join(missing)
            + "。请在GitHub仓库Settings→Secrets配置环境变量，"
            + "或在脚本同目录创建 config.json 提供凭证。"
        )

WAREHOUSE_NAME = "深圳物流仓"

DEVICE_MAP = {
    "111112601290117": "深圳物流仓1F-货品周转区",
    "111112601290118": "深圳物流仓2F-跨境存储区",
    "111112601290121": "深圳物流仓3F-全棉存储区",
    "111112601290119": "深圳物流仓13F-香港存储区",
}

TEMP_MIN = 15.0
TEMP_MAX = 35.0
HUM_MIN = 30.0
HUM_MAX = 80.0
# ==================== 配置区结束 ====================


def fala_get_token(user_key, now_ms):
    return hashlib.md5(f"{user_key}{now_ms}".encode("utf-8")).hexdigest()


def fala_get_device_list():
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
    temp = hum = None
    for sensor in info.get("sensorList", []):
        stype = sensor.get("type")
        if stype == "temperature":
            temp = sensor.get("valueV2") or str(sensor.get("value", 0) / 10)
        elif stype == "humidity":
            hum = sensor.get("valueV2") or str(sensor.get("value", 0) / 10)
    return temp, hum


def parse_device_data(device_list, info_list):
    info_map = {i["deviceNo"]: i for i in info_list}
    base_map = {d["id"]: d for d in device_list}
    results = []
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
    issues = []
    ok = True
    try:
        temp = float(device["temp"])
        if temp > TEMP_MAX:
            issues.append(f"温度偏高 {temp:g}℃（上限{TEMP_MAX:g}℃）")
            ok = False
        elif temp < TEMP_MIN:
            issues.append(f"温度偏低 {temp:g}℃（下限{TEMP_MIN:g}℃）")
            ok = False
    except (TypeError, ValueError):
        issues.append("温度数据缺失")
        ok = False
    try:
        hum = float(device["hum"])
        if hum > HUM_MAX:
            issues.append(f"湿度偏高 {hum:g}%（上限{HUM_MAX:g}%）")
            ok = False
        elif hum < HUM_MIN:
            issues.append(f"湿度偏低 {hum:g}%（下限{HUM_MIN:g}%）")
            ok = False
    except (TypeError, ValueError):
        issues.append("湿度数据缺失")
        ok = False
    return ok, issues


def build_post_content(devices):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = []

    def row(text, bold=False, red=False):
        style = []
        if bold:
            style.append("bold")
        if red:
            style.append("red")
        content.append([{"tag": "text", "text": text, "style": style}])

    row(f"仓库：{WAREHOUSE_NAME}", bold=True)
    row(f"采集时间：{now_str}")
    row("——————————————————")

    for d in devices:
        temp = d["temp"] if d["temp"] is not None else "--"
        hum = d["hum"] if d["hum"] is not None else "--"
        power = f"{d['power']}%" if d["power"] is not None else "--"
        net = "在线" if d["net_status"] == "online" else "离线"

        ok, issues = judge_status(d)

        row(d["region_name"], bold=True)
        row(f" 🌡️ 温度：{temp}℃", bold=True)
        row(f"💧 湿度：{hum}％", bold=True)
        row(f"🔋 电量：{power}", bold=True)
        if ok:
            row("✅ 状态：正常", bold=True)
        else:
            row("⚠️ 状态：异常", bold=True, red=True)
            for issue in issues:
                row(f"　└ {issue}", red=True)
        row(f"📶 联网：{net}", bold=True)
        row("")

    row("——————————————")
    row(f"数据更新时间：{now_str}")

    return {"zh_cn": {"title": "温湿度监测日报", "content": content}}


def feishu_get_token():
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
        check_credentials()
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

        feishu_send_post(FEISHU_CHAT_ID, post_content)
        print("\n✅ 消息已推送到飞书群「深圳仓（物流服务部）」")

    except Exception as e:
        print(f"❌ 推送失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
