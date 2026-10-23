#!/bin/zsh
# Собирает «Запустить CS Elo.app». Код приложения кладётся ВНУТРЬ значка, а таблицы
# сохраняются в ~/Documents/CS Elo — так программа работает, даже если macOS запустила
# скачанную копию из папки только для чтения (App Translocation).
set -e
cd "$(dirname "$0")"
APP="Запустить CS Elo.app"
SRC="$(mktemp -d)/launcher.applescript"
cat > "$SRC" <<'EOF'
on run
	set codePath to (POSIX path of (path to me)) & "Contents/Resources/app/elo_app.py"
	set cmd to "export CS_ELO_RESULTS=\"$HOME/Documents/CS Elo\"; mkdir -p \"$CS_ELO_RESULTS\"; " & ¬
		"if ! /usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then " & ¬
		"(cd \"$CS_ELO_RESULTS\" && PYTHONDONTWRITEBYTECODE=1 exec nohup /usr/bin/python3 " & quoted form of codePath & " --no-browser) > \"$CS_ELO_RESULTS/server.log\" 2>&1 < /dev/null & fi"
	try
		do shell script cmd
	on error errMsg
		display dialog "Не удалось запустить CS Elo:" & return & errMsg buttons {"OK"} default button 1 with icon caution
		return
	end try
	delay 1.5
	open location "http://localhost:8765"
end run
EOF
rm -rf "$APP"
osacompile -o "$APP" "$SRC"
mkdir -p "$APP/Contents/Resources/app"
cp app/elo_app.py app/demo_tournament.json "$APP/Contents/Resources/app/"
codesign --force --sign - "$APP" 2>/dev/null
echo "Собрано: $APP"
