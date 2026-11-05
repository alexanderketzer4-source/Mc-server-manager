# Minecraft Server Manager

Create, run, update and downgrade a Minecraft Java server (Vanilla, Paper, Fabric) from a simple Windows app. No command line needed.

## For users
1. Download **MinecraftServerManager.exe** from the Releases page.
2. Double-click it. (Windows SmartScreen may warn about an unknown publisher: click *More info* -> *Run anyway*.)
3. Click **Create Server**, pick a type and version, then **Download & Create**.
4. Go to **Console** and press **Start**.

You need Java installed (Java 21 for current Minecraft). Get it free from https://adoptium.net .

## For developers
Run from source:

    pip install customtkinter requests pillow
    python main.py

Build the exe: double-click `build_exe.bat` -> `dist\MinecraftServerManager.exe`.
