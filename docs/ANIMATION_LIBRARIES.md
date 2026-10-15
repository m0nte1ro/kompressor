# Animated libraries (Cartoons and Cartoons Filmes)

Kompressor supports four **independent library roots** in filesystem mode.
The two additional sections are **Cartoons** (TV episodes) and **Cartoons Filmes**
(animated films). They appear alongside Movies and Shows in the sidebar.

| Section | Library key | Encoding/preset scope | Default path in CT 110 |
| --- | --- | --- | --- |
| Movies | movie | movie | existing configured Movies path |
| Shows | show | show | existing configured Shows path |
| Cartoons | cartoons | show | `/media/cartoons` |
| Cartoons Filmes | cartoons_filmes | movie | `/media/cartoons-filmes` |

The last two defaults correspond to the Proxmox host bind mounts:

```text
mp3: /mnt/storage/media/cartoons,mp=/media/cartoons
mp4: /mnt/storage/media/cartoons-filmes,mp=/media/cartoons-filmes
```

The app uses **container paths**, never the host-side `/mnt/storage/media`.
Existing saved library paths remain authoritative, and installations whose saved
JSON predates these fields inherit only the new roots from environment defaults.
To disable a root, save an explicitly empty path through Settings.

A single read-only discovery scan processes all configured roots independently.
The root ID uses the original underlying `movie`/`show` media scope plus the
actual path. Encoding jobs still belong to the ordinary movie or show scope and
use the same presets, policy, queue, CPU/GPU workers, source guards, and validated
replacement path. No extra processes or independent queue lanes are created.
Each sidebar section filters the inventory by the corresponding root, without
hiding that library from the shared queue/history or from the API inventory.
No source file is renamed or changed by scanning.

Roots must be absolute, separate and non-overlapping, and none may include
Kompressor's database or workspace. Mount access and filesystem permissions
inside the unprivileged container must be verified before scanning. Settings
paths are applied on the **next scan**, not midway through an active scan.

With the CT 110 bind mounts active, open **Settings → Library paths**, confirm
`/media/cartoons` and `/media/cartoons-filmes`, save, then use **Rescan library**.
The new sections should populate as discovery and ffprobe complete.
