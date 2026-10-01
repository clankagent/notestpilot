#!/usr/bin/env bash
# Run only inside the disposable lab VM. Installs no host services/public routes.
set -euo pipefail
root="${1:?Supply a NEW lab input directory}"
if [[ -e "$root" ]]; then
  echo "Refusing existing input directory: $root" >&2
  exit 1
fi
mkdir -p "$root/tools/steamcmd" "$root/game" "$root/tools/dotnet"
sudo apt-get update -qq
sudo apt-get install -y -qq lib32gcc-s1 unzip libicu78
curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz -o "$root/tools/steamcmd.tar.gz"
tar -xzf "$root/tools/steamcmd.tar.gz" -C "$root/tools/steamcmd"
if ! "$root/tools/steamcmd/steamcmd.sh" +force_install_dir "$root/game" +login anonymous +app_update 3930080 validate +quit; then
  # First-run SteamCMD sometimes needs its app metadata refreshed after self-update.
  "$root/tools/steamcmd/steamcmd.sh" +login anonymous +app_info_update 1 +app_info_print 3930080 +quit > "$root/tools/app-info.txt"
  "$root/tools/steamcmd/steamcmd.sh" +force_install_dir "$root/game" +login anonymous +app_update 3930080 validate +quit
fi
curl -fsSL https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.3/BepInEx_linux_x64_5.4.23.3.zip -o "$root/tools/bepinex.zip"
unzip -q "$root/tools/bepinex.zip" -d "$root/game"
curl -fsSL https://dot.net/v1/dotnet-install.sh -o "$root/tools/dotnet-install.sh"
bash "$root/tools/dotnet-install.sh" --channel 8.0 --install-dir "$root/tools/dotnet"
curl -fsSL https://astral.sh/uv/0.12.12/install.sh -o "$root/tools/uv-install.sh"
UV_INSTALL_DIR="$root/tools/uv" sh "$root/tools/uv-install.sh"
echo "Inputs ready. No game process has been launched."
