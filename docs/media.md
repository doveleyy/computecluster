# Media

How the household media library is served, and why it sits beside the
platform rather than inside it.

## Shape

```text
     media player on the overlay network (browser, phone, TV app)
                         |
                         | HTTPS to the workstation's private name
                         v
    +--------------------------------------------------+
    | LINUX WORKSTATION                                |
    |                                                  |
    |   reverse proxy (Tailscale Serve): TLS only      |
    |          |                                       |
    |          v  loopback                             |
    |   media server container (Jellyfin)              |
    |     settings, cache    -> local disk, writable   |
    |     library            -> /media, read-only      |
    |                              ^                   |
    |   on-demand NAS mount -------+  read-only        |
    +--------------------------------------------------+
                         |
                         | SMB, dedicated read-only account
                         v
    +--------------------------------------------------+
    | NAS: `Media` share, separate from every other    |
    | share and from every platform account            |
    +--------------------------------------------------+
```

## Why the workstation, not the host

The always-on host is small and low power, and its job is to coordinate. Video
is the opposite workload: large sequential reads, sustained bandwidth, and
occasionally CPU-heavy transcoding. Putting that on the host would compete with
the job queue and every household application for the one machine they all
depend on.

The workstation has the CPU and the network headroom, and it already runs
containers as a compute worker. The media server is simply a second, unrelated
container on it.

## A second private entrance

Every machine on the overlay network has its own stable private name, and each
can run its own small reverse proxy that terminates HTTPS for that name. The
host's proxy is the platform's front door; the workstation runs another, for the
media server only.

Routing media through the host's proxy instead would work, but every byte of
every stream would then cross the host twice — in from the workstation and out
to the player — for no gain in security or convenience. A separate entrance
keeps video traffic between the player and the machine that serves it.

The same rules apply as on the host:

- the media server binds to loopback, so its proxy is the only way in;
- the entrance is private to the overlay network, never a public tunnel; and
- network privacy is not authentication, so the media server still requires
  its own login.

It does not use the platform's identity system. Jellyfin has its own accounts
and does not read proxy identity headers, so a media login is separate from a
platform account. That keeps the media server a neighbour on the network
rather than a platform service with access to platform data.

## Read-only, three times over

The media server can read the library and cannot change it. That is enforced
by three independent layers, so no single mistake grants write access:

1. **A dedicated file-server account** with read-only access to the `Media`
   share and no access to any other share or service on the NAS.
2. **A read-only mount** on the workstation, made with that account.
3. **A read-only bind** of that mount into the container.

Its settings, metadata, and transcoding cache are the only things it writes,
and they stay on the workstation's local disk. They are rebuildable, and like
every live database here they are kept off the network share.

The mount is made on demand at first access, and the media server starts only
after it is available, so a slow or absent NAS delays the media server instead
of letting it start against an empty directory and forget the library.

## Adding media

Copy files into the `Media` share from any file app over SMB. The media server
sees them immediately on disk, but it does **not** watch the share for changes:
change notifications are not reliable over a network mount. New files appear in
the library after the next library scan, either on its schedule or started by
hand from the media server's dashboard.

File-server recycle bins deserve one warning. When a share's recycle bin is on,
a deleted file is moved into a hidden folder inside that same share, where a
library scan can find it again. Either disable the recycle bin on the `Media`
share or restrict it to administrators, so the media server's account cannot
see deleted files.

## Limits

- The workstation is a laptop. When it sleeps or is off, media is unavailable;
  nothing else on the platform is affected.
- Media accounts are separate from platform accounts and are managed in the
  media server itself.
- The media server's settings live on one disk. Losing it means rebuilding the
  library configuration, not losing any media.
