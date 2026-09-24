@echo off
chcp 65001 >nul
rem ============================================================================
rem  放行局域网访问桥接服务的入站规则（TCP 8787）。
rem
rem  为什么需要这一步：Windows 防火墙的入站规则是按「程序路径」匹配的。以前
rem  弹窗放行过的那次是 base python（versions\3.13.12\python.exe），而
rem  run_watch_server.bat 现在用的是 venv 里的另一个可执行文件 —— 两者不是
rem  同一个文件，规则匹配不上，局域网来的包就被默认丢弃了。
rem
rem  注意：这点在电脑本机上测不出来。连 127.0.0.1 走环回，环回不过防火墙，
rem  所以本机怎么测都是通的，只有手表从 Wi-Fi 过来才会失败。
rem
rem  规则按端口放行而不是按程序放行，这样换解释器、换虚拟环境都不会再失效。
rem  暴露面只有这一个端口，而协议本身要求令牌 + AES-256-GCM，风险可控。
rem
rem  用法：右键本文件 →「以管理员身份运行」。会弹一次 UAC。
rem ============================================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 需要管理员权限，正在申请提升……
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ==================================================
echo   为抖音手表桥接放行 TCP 8787 入站
echo ==================================================
echo.

netsh advfirewall firewall delete rule name="Douyin Watch Bridge (TCP 8787)" >nul 2>&1
netsh advfirewall firewall add rule ^
    name="Douyin Watch Bridge (TCP 8787)" ^
    dir=in action=allow protocol=TCP localport=8787 profile=any
if %errorlevel% neq 0 (
    echo [失败] 规则没能加上，请把上面的报错截图发出来。
) else (
    echo [完成] 已放行。手表现在可以用 局域网IP:8787 连进来了。
)

echo.
echo 当前状态：
netsh advfirewall firewall show rule name="Douyin Watch Bridge (TCP 8787)" | findstr /C:"规则名称" /C:"已启用" /C:"操作" /C:"本地端口"
echo.
echo 本机地址（手表上要填的就是 这个IP:8787）：
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { '  ' + $_.IPAddress + '  (' + $_.InterfaceAlias + ')' }"
echo.
pause
