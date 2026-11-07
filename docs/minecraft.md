# Minecraft fishbowl

This adds a local body and a window into AI-AM's activity. Each character uses the four mind roles together: Manas handles observations, Buddhi chooses actions, Ahamkara tracks identity and commitments, and Chitta handles memory. Two characters have separate mind data; they are not the four roles split into four NPCs.

The bridge connects Mineflayer to a private Minecraft Java server. The Python supervisor runs the minds and serves the dashboard. The dashboard shows what the characters can observe, their decisions, and their viewers. This is a behavioral research platform, not evidence that the characters have subjective experience.

## Before starting

Use a **local drive other than C:** for the repository, Python installation and virtual environment, Node installation and dependencies, Java installation, server/world, model storage, application data, caches, and logs. The launcher rejects C: paths, UNC paths, and path junctions or symbolic links. A virtual environment on T: whose base Python lives on C: is also rejected.

You need existing installations of:

- Python 3.10 or newer and this project's Python dependencies.
- Node.js 22 or newer and the dependencies in `minecraft/package.json`.
- Java 21 and a **Minecraft Java 1.21.4** server.
- A running local Ollama service with the model named by your mind configuration already installed. Keep Ollama's model storage on your chosen drive too. The launcher does not relocate or configure an already-running Ollama service's own logs, cache, or model directory.

The launcher installs nothing, downloads no models or server files, and does not accept the Minecraft EULA. Obtain the server through the [official Minecraft server distribution](https://www.minecraft.net/en-us/download/server), matching the configured 1.21.4 version. Review the [Minecraft EULA](https://www.minecraft.net/en-us/eula) yourself before accepting it.

Example layout:

| Location | Purpose |
| --- | --- |
| `T:\AI-AM` | Repository, including `.venv` and `minecraft\node_modules` |
| `T:\Runtimes\Python`, `T:\Runtimes\node`, `T:\Runtimes\jdk-21` | Runtime installations |
| `T:\AIAMMinecraftServer` | Vanilla server JAR, configuration, world, server logs |
| `T:\AIAMMinecraftData` | Isolated mind databases, runtime configuration, logs, caches |

For an explicit dependency installation, run commands like these after adapting the paths. These commands download dependencies; the launcher itself does not.

```powershell
Set-Location T:\AI-AM
New-Item -ItemType Directory -Force T:\AIAMMinecraftData\temp, T:\AIAMMinecraftData\cache | Out-Null
$env:TEMP = 'T:\AIAMMinecraftData\temp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = 'T:\AIAMMinecraftData\cache\pip'
$env:NPM_CONFIG_CACHE = 'T:\AIAMMinecraftData\cache\npm'
$env:PYTHONDONTWRITEBYTECODE = '1'
& T:\Runtimes\Python\python.exe -m venv T:\AI-AM\.venv
& T:\AI-AM\.venv\Scripts\python.exe -m pip install -e .
& T:\Runtimes\node\npm.cmd install --prefix T:\AI-AM\minecraft
```

## Start the private server separately

Prepare a vanilla server in `T:\AIAMMinecraftServer`. After your own EULA decision, ensure `eula.txt` contains `eula=true` and `server.properties` explicitly contains:

```properties
server-ip=127.0.0.1
server-port=25565
online-mode=false
level-name=world
gamemode=survival
force-gamemode=true
```

The offline identities are for this private loopback experiment. Keep the server bound to `127.0.0.1`; do not expose it through a proxy, tunnel, or port forwarding. Use a simple world name without spaces or path separators. The bridge requires survival mode; forcing that mode also applies it to previously saved player identities. Adjust ordinary game settings, such as difficulty, yourself.

Start Java from the server directory in its own console. The memory limits below are examples, not measured requirements:

```powershell
Set-Location T:\AIAMMinecraftServer
New-Item -ItemType Directory -Force .\temp | Out-Null
$env:TEMP = 'T:\AIAMMinecraftServer\temp'
$env:TMP = $env:TEMP
& T:\Runtimes\jdk-21\bin\java.exe '-Djava.io.tmpdir=T:\AIAMMinecraftServer\temp' -Xms1G -Xmx2G -jar .\server-1.21.4.jar nogui
```

The launcher checks the supplied server directory and its configuration, and checks that the loopback port is reachable. It cannot prove that an already-running Java process uses that directory, runtime, or temporary directory. Start that process with the paths you intend. The launcher never changes or stops the Java server. Enter `stop` in the Java console to save and shut it down cleanly.

## Start AI-AM

In a second PowerShell window:

```powershell
Set-Location T:\AI-AM
.\start_minecraft.ps1 -DataDir T:\AIAMMinecraftData -ServerDir T:\AIAMMinecraftServer -PythonExe T:\AI-AM\.venv\Scripts\python.exe -NodeExe T:\Runtimes\node\node.exe
```

Add `-CheckOnly` to run preflight without starting services or writing launcher files. Add `-Agents 1` for only AI_AM; the default adds Mira. Use `-MindConfig T:\path\mind.json` for an existing mind configuration. The two characters still receive separate data directories. Start with one character if inference is slow.

Open **http://127.0.0.1:8765**. The bridge uses port 8766; viewers use 8767 and 8768. Override these with `-Port`, `-BridgePort`, `-ViewerPort`, and `-MinecraftPort`; they must be distinct, at least 1024, and match the server configuration.

Launching does not start an autonomous goal. Use the dashboard to give a bounded goal. Service readiness is separate from characters successfully joining the world. Check their connection status; starting a goal can reconnect or respawn a character. A local model may make poor plans or produce invalid actions, so observe a short run first.

Ctrl+C stops only the bridge and supervisor started by that launcher. It does not stop Minecraft or Ollama. The bridge's heartbeat lease also stops active movement if the supervisor disappears.

Application logs are under `DataDir\logs`; generated configuration is under `DataDir\runtime`. Caches and temporary files are redirected to `DataDir\cache` and `DataDir\temp`. The random bridge token is inherited through the process environment and is not written into the configuration or printed.

A mind checkpoint backs up a character's mind database. It is **not a Minecraft world snapshot**. Save and back up the world separately through the server. Neither a checkpoint nor a convincing sentence from a character establishes a conscious experience.
