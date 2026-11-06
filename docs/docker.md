# Docker

devkit runs Docker on [Colima](https://github.com/abiosoft/colima), a lightweight Linux VM, with the Docker CLI,
Compose and Buildx from Homebrew. No Docker Desktop licence is needed.

## Start and stop

```bash
dev docker up              # create (first time) and start the VM, then print the Docker server summary
dev docker status          # colima status + docker system df
dev docker down            # stop the VM (frees its memory)
dev docker prune           # docker system prune -f + docker builder prune -f (never volumes)
dev docker autostart-on    # start Colima at login (brew services); autostart-off to undo
```

## VM settings

From `docker` in `~/.config/devkit/config.json`:

| Setting | Default | Notes |
|---|---|---|
| `cpu` | `6` | Changeable on a later `colima start` |
| `memory` | `12` (GB) | Changeable on a later start |
| `disk` | `80` (GB) | Sparse, only used space is taken; can only grow |
| `vm_type` | `vz` | Apple Virtualization framework |
| `rosetta` | `true` | With `vz`, runs `linux/amd64` images through Rosetta |
| `mount_type` | `virtiofs` | Fast host folder sharing |

`dev docker up` runs, for example:

```bash
colima start --cpu 6 --memory 12 --disk 80 --vm-type vz --mount-type virtiofs --vz-rosetta
```

## CLI plugins and credentials

`dev install --only docker` merges into `~/.docker/config.json`:

```json
{
  "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"],
  "credsStore": "osxkeychain"
}
```

so `docker compose` and `docker buildx` are found, and registry logins are stored in the macOS keychain (when
`docker-credential-osxkeychain` is installed; an existing `credsStore` is kept).

## SDKs and Testcontainers

Colima sets the `colima` Docker context, which the CLI uses. Libraries (Testcontainers, docker-java, docker-py)
read `DOCKER_HOST` instead, so devkit sets it in your shell, in Claude Code's `env` and in Codex's
`shell_environment_policy`:

```bash
DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
```

The second variable tells Testcontainers which socket path to mount inside its helper containers (Ryuk).

## amd64 images

With `vz` and Rosetta, `linux/amd64` images run without extra setup:

```bash
docker run --rm --platform linux/amd64 alpine uname -m     # x86_64
```

## Rules for agents

The global instructions tell agents to start Docker with `dev docker up`, never prune volumes, avoid pulling images or
starting long-running containers the task does not need, and stop the containers they started. The automatic cleanup
prunes only dangling images and build cache older than 7 days ([Disk and cleanup](disk-and-cleanup.md)).

See also: [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)
