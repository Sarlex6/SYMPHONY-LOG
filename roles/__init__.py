"""Role Management / Cross-Platform Synchronization system.

Google Sheets (the PERSONNEL sheet) is the authoritative source of truth.
Discord and Roblox are synchronization *targets* — they conform to the sheet,
never the other way around.

Layering (top calls down, never sideways or up):

    commands.py / angela_bridge.py     entry points (Discord slash cmds, Angela)
        └─ service.py                  RoleManagerService — the only mutator
            ├─ permissions.py          centralized authorization
            ├─ repository.py           UserRecord read/write
            │   ├─ layout.py           row/category placement + sorting
            │   └─ sheets_gateway.py   raw PERSONNEL worksheet I/O
            └─ sync_queue.py           retryable outbound sync
                ├─ discord_sync.py
                └─ roblox_sync.py

    poller.py    background change detection (sheet -> sync_queue)
    config.py    rank / branch / server / permission configuration
"""
