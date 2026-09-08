# Xrob Music + NAS storage

Xrob Music does not mount SMB/NFS itself. On Home Assistant OS, mount the NAS through Home Assistant's Network Storage feature, then expose `/media` to the add-on.

## Your setup

- Home Assistant OS: `192.168.1.73`
- NAS: `192.168.1.162`
- Add-on port: `8099`
- Recommended HA network-storage mount name: `xrob-music`
- Xrob Music library path: `/media/xrob-music`

## 1. Mount the NAS in Home Assistant

In Home Assistant:

1. Open **Settings → System → Storage**.
2. Add a **Network Storage**.
3. Server: `192.168.1.162`
4. Select the NAS protocol used by your share (normally SMB/CIFS).
5. Enter the NAS share name and NAS credentials.
6. Set the mount name to `xrob-music`.
7. Use the storage for media/files as appropriate.

The important result is that Home Assistant exposes the mounted share as:

`/media/xrob-music`

The exact NAS share name is not hard-coded because it is specific to the NAS configuration.

## 2. Configure the add-on

The add-on configuration already contains:

```yaml
music_path: "/media/xrob-music"
```

The add-on maps `/media` read/write into the container.

Restart Xrob Music after mounting the NAS.

## 3. Verify

Open Xrob Music on:

`http://192.168.1.73:8099`

Open **Settings → Library Storage**.

You should see:

- `/media/xrob-music`
- Library available
- Used space
- Free space
- Rescan button

If the NAS is disconnected, Xrob Music intentionally refuses to silently create an empty local replacement directory. This prevents a NAS outage from appearing as an empty library.

## Important permissions

The Home Assistant network-storage mount must be writable by the add-on. Xrob Music needs write access because downloads, metadata processing, cover cache, task database and settings use the configured library directory.

## OpenSubsonic / Arpeggi

Xrob Music is exposed on port `8099`.

For a client on the LAN, use:

`http://192.168.1.73:8099`

Do not use the old `8100` port.
