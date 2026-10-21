// 复制成 secrets.h 再改。secrets.h 不要发给别人、不要传到公开仓库。
#pragma once

// 板子只认 2.4GHz Wi-Fi。iPhone 热点要打开「最大兼容性」才是 2.4GHz。
// 按顺序试，连上哪个用哪个。
#define WIFI_SSID_1 "your-2.4GHz-wifi"
#define WIFI_PASS_1 "your-password"
#define WIFI_SSID_2 ""
#define WIFI_PASS_2 ""

// 电脑上跑的中继（relay/relay.py）。电脑和板子连同一个热点后，
// 在电脑上运行 `ipconfig getifaddr en0` 看 IP，填在这里。
#define RELAY_BASE "http://172.20.10.2:8787"

#define DEVICE_ID "senses-1"
