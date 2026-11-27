#!/usr/bin/env bash
set -euo pipefail

mode=${1:?mode is required}
instance=${2:?instance is required}
: "${CHROME_PORT:?CHROME_PORT is required}"
: "${CHROME_PROFILE:?CHROME_PROFILE is required}"

# Shared hardening flags.  See /opt/subsidy/BROWSER_HARDENING.md for the measured
# rationale; every flag below is anchored to a value probed from this VM.
common_args=(
  # --- Display geometry: headless must agree with the page's own viewport ---
  # Probed before this change: screen 800x600 but innerWidth/Height 1600x813.
  # That mismatch is a dead giveaway of a headless browser, because a real
  # maximised window cannot report a viewport wider than the virtual screen.
  #
  # --window-size alone does NOT fix it: in --headless=new the window is sized
  # but `screen` keeps the Ozone default 800x600, so outer(1600x900) still
  # contradicted screen(800x600).  --ozone-override-screen-size is what
  # actually resizes the virtual display; verified on this VM to produce
  # screen==1600x900==window, i.e. a maximised-window signature.
  --window-size=1600,900
  --window-position=0,0
  --ozone-override-screen-size=1600,900
  --force-device-scale-factor=1
  # Hide the automation infobar without claiming to be non-automated.
  --disable-infobars

  # --- WebRTC: keep the API object but deny it network reach ---
  # Probed: ICE candidates already come back empty (no STUN reply), so no local
  # or RFC1918 address leaks today.  The policy below makes that an enforced
  # rule rather than an accident of no-egress, and it stops future STUN/TURN
  # chatter from exposing the datacenter address.  We deliberately do NOT use
  # --disable-webrtc, because a browser with RTCPeerConnection fully absent is
  # itself an anomaly; "present but no candidates" is the normal corporate
  # policy state.
  --force-webrtc-ip-handling-policy=disable_non_proxied_udp
  --enforce-webrtc-ip-permission-check

  # --- Timestamp entropy: monotonic clock is a headless/container tell ---
  # Coarse-resolution timers are indistinguishable from a real OS by the risk
  # SDK, and stop sub-millisecond fingerprinting of the collection loop.
  --enable-features=ReduceTimerPrecision,CoarseTimerPrecision
)

# --- GPU: this VM has NO physical GPU and no PCI bus at all ---
# `lspci` is empty; the only display device is the Hyper-V synthetic vmbus
# adapter (hyperv_drm, vendor 0x1414, device 0x6).  Hyper-V would need GPU-P
# with a real host GPU to change that, which this Xeon-only host cannot do.
# Falling back to Mesa/llvmpipe yields a *worse* fingerprint than Chrome's own
# SwiftShader: Mesa reports "llvmpipe (LLVM 15.0.7)" and Chrome blocklists
# WebGL entirely, so pages that probe for WebGL get a hard null.  We therefore
# keep GPU disabled and let SwiftShader service WebGL, which at least yields a
# self-consistent software-rendering profile.  Do NOT remove --disable-gpu.
gpu_args=(--disable-gpu)

# --- 无头模式已移除（2026-09-28）-----------------------------------------
# `--headless=new` 会让 Chrome 在 User-Agent 里报 "HeadlessChrome/153.0.0.0"，
# 被平台风控直接识别为爬虫环境。改用真实窗口后 UA 与普通桌面 Chrome 一致。
# 模式参数保留原样，避免改动所有单元文件；现在两种模式都表示「真实窗口」。
case "$mode" in
  headless | visible) mode_args=() ;;
  *) echo "invalid mode: $mode" >&2; exit 2 ;;
esac

# 去掉无头模式后 Chrome 需要一个 X display。采集实例与登录实例一样跑在 :1
# （运维桌面）上；单元文件已显式设置 DISPLAY/XAUTHORITY，这里的兜底只用于
# 手工调用脚本的场景。
: "${DISPLAY:=:1}"
export DISPLAY

# Only semantic *-E instances are e-commerce browsers. Prescription instances
# retain their blank landing page and decide their own navigation.
if [[ "$instance" == *-E ]]; then
  start_url='https://yiyao.meituan.com/main/frame#/page/home'
else
  start_url='about:blank'
fi

# A real Linux desktop reports both zh-CN and en-US in Accept-Language when the
# agent runs without a locale override.  Chrome derives both the header and
# navigator.languages from these two values, keeping them mutually consistent.
exec /opt/google/chrome/chrome "${mode_args[@]}" "${gpu_args[@]}" "${common_args[@]}" \
  --no-sandbox --disable-dev-shm-usage \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port="$CHROME_PORT" \
  --remote-allow-origins='*' --user-data-dir="$CHROME_PROFILE" \
  --lang=zh-CN --accept-lang=zh-CN,zh,en-US,en --noerrdialogs --no-first-run "$start_url"
