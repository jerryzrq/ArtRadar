# 以管理员身份运行一次：允许手机访问 ArtRadar 的 8765 端口
New-NetFirewallRule -DisplayName "ArtRadar LAN 8765" `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 `
    -Profile Private
