# Minecraft fishbowl

This adds a local body and a window into AI-AM's activity. Each character uses the four mind roles together: Manas handles observations, Buddhi chooses actions, Ahamkara tracks identity and commitments, and Chitta handles memory. Two characters have separate mind data; they are not the four roles split into four NPCs.

The bridge connects Mineflayer to a private Minecraft Java server. The Python supervisor runs the minds and serves the dashboard. The dashboard shows what the characters can observe, their decisions, and their viewers. This is a behavioral research platform, not evidence that the characters have subjective experience.

## Before starting

Use a **local drive other than C:** for the repository, Python installation and virtual environment, Node installation and dependencies, Java installation, server/world, model storage, application data, caches, and logs. The launcher rejects C: paths, UNC paths, and path junctions or symbolic links. A virtual environment on T: whose base Python lives on C: is also rejected.

You need a running local Ollama service with the model named by your mind configuration already installed. Keep Ollama's model storage on your chosen drive. An already-running Ollama service manages its own logs, cache, and model directory.

### 1. Prepare the portable runtimes

From a writable non-C checkout, preview the setup and then run it. Choose a new directory for the runtime installation.

```powershell
Set-Location T:\AI-AM
.\prepare_minecraft.ps1 -DataDir T:\AIAMMinecraftData -Plan
.\prepare_minecraft.ps1 -DataDir T:\AIAMMinecraftData
```

The setup downloads and verifies Python 3.12, Node.js 22, and Java 21 archives. It creates a Python virtual environment, installs the project's Python dependencies, and installs the bridge's npm dependencies into `minecraft\node_modules` in the checkout. Start with a checkout that has no `minecraft\node_modules`. Setup puts its caches and temporary files under the runtime directory and records the exact paths in `prepared.json`.

### 2. Obtain Minecraft Java 1.21.4

Use the cross-platform server JAR link on the [official 1.21.4 release page](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-4). Save the JAR as `T:\AIAMMinecraftServer\server-1.21.4.jar` for this example. Review the [Minecraft EULA](https://www.minecraft.net/en-us/eula) before deciding whether to accept it. Server setup and EULA acceptance are separate from preparing the runtimes.

### 3. Start the private server

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
$setup = Get-Content -Raw T:\AIAMMinecraftData\prepared.json | ConvertFrom-Json
Set-Location T:\AIAMMinecraftServer
New-Item -ItemType Directory -Force .\temp | Out-Null
$env:TEMP = 'T:\AIAMMinecraftServer\temp'
$env:TMP = $env:TEMP
& $setup.java_exe '-Djava.io.tmpdir=T:\AIAMMinecraftServer\temp' -Xms1G -Xmx2G -jar .\server-1.21.4.jar nogui
```

The launcher checks the supplied server directory and its configuration, and checks that the loopback port is reachable. It cannot prove that an already-running Java process uses that directory, runtime, or temporary directory. Start that process with the paths you intend. The launcher never changes or stops the Java server. Enter `stop` in the Java console to save and shut it down cleanly.

### 4. Start AI-AM

In a second PowerShell window:

```powershell
Set-Location T:\AI-AM
$setup = Get-Content -Raw T:\AIAMMinecraftData\prepared.json | ConvertFrom-Json
.\start_minecraft.ps1 -DataDir $setup.mind_data_dir -ServerDir T:\AIAMMinecraftServer -PythonExe $setup.python_exe -NodeExe $setup.node_exe
```

Add `-CheckOnly` to run preflight without starting services or writing launcher files. Add `-Agents 1` for only AI_AM; the default adds Mira. Use `-MindConfig T:\path\mind.json` for an existing mind configuration. The two characters still receive separate data directories. Start with one character if inference is slow.

Open **http://127.0.0.1:8765**. The bridge uses port 8766; viewers use 8767 and 8768. Override these with `-Port`, `-BridgePort`, `-ViewerPort`, and `-MinecraftPort`; they must be distinct, at least 1024, and match the server configuration.

Launching does not start an autonomous goal. Use the dashboard to give a bounded goal. Service readiness is separate from characters successfully joining the world. Check their connection status; starting a goal can reconnect or respawn a character. A local model may make poor plans or produce invalid actions, so observe a short run first.

Ctrl+C stops only the bridge and supervisor started by that launcher. It does not stop Minecraft or Ollama. The bridge's heartbeat lease also stops active movement if the supervisor disappears.

Application logs are under `DataDir\logs`; generated configuration is under `DataDir\runtime`. Caches and temporary files are redirected to `DataDir\cache` and `DataDir\temp`. The random bridge token is inherited through the process environment and is not written into the configuration or printed.

A mind checkpoint backs up a character's mind database. It is **not a Minecraft world snapshot**. Save and back up the world separately through the server. Neither a checkpoint nor a convincing sentence from a character establishes a conscious experience.

## Verify your first run

Start with one character and give it the goal “Collect one visible log, craft planks, and stop.” Watch the inventory and action results. Collection should remove the observed log and add one to inventory. Crafting should consume the log and add four planks. A timeout leaves an unknown outcome and stops the run. Check the current inventory before starting a new goal.

Once gathering and crafting work on your server, start a fresh Survival world during daylight and watch the character try to stay fed and sheltered until dawn. Record the game time, deaths, and any operator help so you can compare runs.

The hosted live test exercises the production bridge and camera through Minecraft 1.21.4 packets using the open-source Flying Squid server. Its flat fixture tests joining, looking, movement, collection, camera updates, and cancellation. Crafting receipts are reported separately because the compatibility server's inventory behavior can differ from vanilla.
