# Taxonero

**English** · [Français](README.fr.md)

Self-hosted AI species identification for your photos (birds, mammals, reptiles, amphibians, insects, spiders,
fish, plants, fungi and lichens): an [Immich](https://immich.app) library, a local folder or a photo application
(darktable, Lightroom Classic, RawTherapee, RapidRAW). **Your pictures are never modified.**

<table>
<tr>
<td width="50%"><a href="docs/screenshots/en/viewer.jpg"><img src="docs/screenshots/en/viewer.jpg" alt="Viewer: subject, candidates and range"></a><br><sub>Viewer: subject, candidates and range</sub></td>
<td width="50%"><a href="docs/screenshots/en/compare.jpg"><img src="docs/screenshots/en/compare.jpg" alt="Comparison with reference photos"></a><br><sub>Comparison with reference photos</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/screenshots/en/species.jpg"><img src="docs/screenshots/en/species.jpg" alt="Species, Grid view by group"></a><br><sub>Species, Grid view by group</sub></td>
<td width="50%"><a href="docs/screenshots/en/tree.jpg"><img src="docs/screenshots/en/tree.jpg" alt="Species, Tree view: kingdom > phylum > class > order > family > species"></a><br><sub>Species, Tree view: kingdom > phylum > class > order > family > species</sub></td>
</tr>
<tr>
<td width="50%"><a href="docs/screenshots/en/photos.jpg"><img src="docs/screenshots/en/photos.jpg" alt="Photos, filtered by status and group"></a><br><sub>Photos, filtered by status and group</sub></td>
<td width="50%"><a href="docs/screenshots/en/species-page.jpg"><img src="docs/screenshots/en/species-page.jpg" alt="Species page: map and article"></a><br><sub>Species page: map and article</sub></td>
</tr>
<tr>
<td colspan="2" align="center"><a href="docs/screenshots/en/stats.jpg"><img src="docs/screenshots/en/stats.jpg" width="50%" alt="Statistics"></a><br><sub>Statistics</sub></td>
</tr>
</table>

<sub>Test photos: authors and licenses in [docs/screenshots/CREDITS.md](docs/screenshots/CREDITS.md).</sub>

- Detects the subjects in each picture (boxes), identifies the species, and checks it against geography
  (does this species live where the photo was taken?).
- Photos and **videos** (the key frame Immich picks for each video is analyzed like a photo).
- Confirms the safe cases automatically: high confidence, species observed nearby, no other credible
  candidate, and never in a group where the model is often wrong (spiders, fish, then based on your reviews).
- Domestic animals (dogs, cats, horses, cattle, chickens) are skipped by default, so family photos do not
  fill the tree of life.
- Everything the dashboard shows (species sheets, names, range maps, articles) is cached locally by the
  sync, so reviewing never waits for the network. The "Sync" button fetches new photos from Immich,
  analyzes them, writes tags and refreshes local data, with a progress bar.
- Analysis in two phases running at the same time, each with its own progress bar: the AI part (detection,
  identification) goes through the queue at full GPU speed while the geographic check (GBIF) follows on its
  results; the GPU is released as soon as the AI part is over. Presence around
  a point costs one GBIF request per 1° area for all species at once (cached 180 days), not one per candidate.
- Dashboard styled like Immich (same colors, font, sidebar): Photos page with filters (to review,
  auto-confirmed, confirmed by me, rejected, no detection) and a keyboard-driven full-screen viewer;
  clickable boxes that open the Wikipedia or Wikispecies article in a side panel; common names in
  15 languages; boxes drawn by hand; Species page (Grid view per group, or Tree view of every branch, kingdom > phylum > class >
  order > family > species; filters by group and IUCN Red List);
  **a page for each species** (main photo you can choose with the star on its photos, names, taxonomy,
  GBIF range map with your observations,
  Wikipedia / Wikispecies article); **a page for each higher rank** (kingdom, phylum, class, order, family, genus: its species, combined
  range map, article), reached from the classification of a species, the Tree view or a group header; statistics (accumulation curve, seasonality, map of all observations,
  countries, Red List, gear, review summary); CSV export.
- Search bar with suggestions while typing: species and ranks, common names in every language and scientific
  names, accents ignored; arrow keys and Enter to open.
- **Bursts**: photos from the same camera taken less than 2 min apart (and less than 1 km), at most 10 min
  from first to last (a long walk is not a burst). In the grid, one
  "×N" tile per burst ("Stacked bursts" toggle): selecting it takes the whole burst. Detections awaiting review
  are smoothed as MegaDetector's post-processing does: an isolated species takes the species suggested on at
  least 4 photos of the burst, if it is among its candidates with at least a quarter of the probability of its own
  suggestion (a confident model is not overruled by a marginal candidate); a genus, family or order is refined by the burst's
  species it contains (at least 2 photos). The viewer says so ("suggested by the burst; the model suggested …").
  Burst photos look alike and often repeat the same error: smoothing fixes inconsistencies without making
  automatic confirmation more permissive.
- **Location set by hand** for photos without GPS (camera without GPS, scans): one click on the map from the
  viewer, or for a whole selection ("Location…" button), with "same place as last time". Detections awaiting
  review are re-ranked right away with that location; a real GPS position is never replaced.
- **Compare with photos** (viewer and Identify page): the subject next to reference photos of each
  candidate, those iNaturalist features on the taxon page (Creative Commons licences, author shown,
  images loaded from iNaturalist).
  The photo's location is shown, with a button to set it when missing, and each candidate says whether it lives in
  the region. Keyboard: `1`-`5` candidate, `←` `→` reference photo, `⏎` choose the species.
- **Identify** page: drop (or paste) an image to have it identified without storing it, with its range
  map and Wikipedia article.
- Fast review: moves to the next photo automatically; select several photos (tick on the photo, long press
  on touch screens, Ctrl/Shift + click, mouse drag, a whole month, every photo of the filter, or the inverse of the
  selection), wherever photos appear, including species and rank pages, to confirm, reject, give them the same
  species, set a location, send them back to review, re-analyse, delete or export to CSV in bulk; a **Back** button
  in the top bar that follows your path and restores the filter, position and open photo; clickable date, place,
  camera and figures; undo (U) that lasts the whole session;
  date scrubber like Immich's (hover shows the month; click or drag jumps there; scrolling back up after a jump loads newer photos), a pause button for AI analysis (frees the processor and memory, kept across restarts), arrow keys in the grid;
  zoom in the viewer (wheel, click, pinch, Z on the selected subject); for a **burst** (same camera, photos less than
  2 min apart), the same taxon is confirmed in one click on the other photos where it is suggested.
  Filters by group, sort by most or least certain first, and for each detection left to review, the
  reason it was not confirmed automatically.
- Interface in 12 languages (French, English, German, Spanish, Italian, Dutch, Portuguese, Catalan,
  Polish, Swedish, Japanese, Chinese), chosen in **Settings > General**; the same language is used for
  species common names and Wikipedia articles. Breton, Basque and Occitan are available for species
  names, with the interface in French. Tags written to Immich follow `NAMES_LANG`.
- In the review sheet, a mini map shows the candidate's range and where the photo was taken; hovering
  another candidate shows its range, to judge plausibility at a glance.
- Writes **hierarchical tags** to Immich (`Nature/Aves/Passeriformes/Paridae/Parus/Great Tit (Parus major)`)
  and a block in the description (name + Wikipedia link), only for confirmed detections.

Inspired by [Specio](https://specio.life), free and self-hosted. Taxonero is an independent project, not
affiliated with Immich. The name comes from *taxon*, a group of living things (species, genus, family…).

## Safety principles

The tool is a separate service that talks to Immich through its public API only.

| Guarantee | How |
|---|---|
| No access to Immich's database, files or containers | Separate Docker service, API only |
| Cannot delete photos | API key limited to 8 permissions, without `asset.delete` or `asset.upload`; a missing permission switches the tool to read-only, an extra one (or "all") is flagged |
| API key protected | Never sent back to the browser (only its last 4 characters); stored in `data/` (or in `.env`), left out of the daily backups |
| No request outside the list | Frozen API contract (`immich/contract.py`): any other request is refused before it is sent |
| An Immich upgrade breaks nothing | At startup, the server's OpenAPI spec is compared with the contract; on any blocking difference, the tool goes **read-only** |
| Nothing is written by default | `DRY_RUN=true` by default |
| Your tags and text are untouchable | The tool only removes what it wrote itself (tracked in its database); in the description, only the `⟦nature⟧…⟦/nature⟧` block belongs to it |
| Everything is reversible | **Settings > Photo source > Undo what was written to Immich** (or `taxonero uninstall --apply`) removes exactly what was written, then suspends writes until you resume them; a tag created by the tool that became empty (species corrected) is deleted, unless you reused it |
| Dashboard protected | Password (HTTP Basic), attempt limit per IP address and refusal of write requests coming from another site (CSRF); fonts and libraries served locally. HTTP Basic is not encrypted: outside a trusted network, use a TLS proxy and set `FORWARDED_ALLOW_IPS` (see `.env.example`) |
| Photo deleted in Immich | Hidden in the dashboard (reviews kept if it comes back from the trash), never synced again |
| Photo you just edited | Left alone for 10 min (see "Known limitations") |

## Models

| Step | Model | License |
|---|---|---|
| Wildlife detection | [MegaDetector v6](https://github.com/microsoft/Pytorch-Wildlife), RT-DETR variant `MDV6-apa-rtdetr-e` | Apache 2.0 |
| Plant, fungus and small animal detection | [OWLv2](https://huggingface.co/google/owlv2-base-patch16-ensemble) (open vocabulary: "a flower", "a mushroom", "an insect"…) | Apache 2.0 |
| Identification | [BioCLIP 2](https://huggingface.co/imageomics/bioclip-2) (ViT-L/14, TreeOfLife-200M, ~950,000 taxa) | MIT |
| Taxonomy, names, IUCN, occurrences | [GBIF](https://www.gbif.org/developer/summary) | CC0 / CC-BY data |
| Multilingual names, Wikipedia and Wikispecies links | Wikidata (property P846, GBIF identifier) | CC0 |
| Range maps | [GBIF maps API](https://techdocs.gbif.org/en/openapi/v2/maps): vector tiles read once on a fixed hexagonal grid (~25 km in Europe for a species or genus, ~50 km for higher ranks), snapped to GBIF's exact grid so the cells line up; when zooming in, the tile of the displayed zoom is fetched on the fly (cells stay ~11 px on screen, cached 30 days, local grid as fallback); field observations only, on a near-white base map | CC0 / CC-BY |
| Reference photos (comparison) | [iNaturalist API](https://api.inaturalist.org/v1/docs/): photos featured on the taxon page, Creative Commons licences only | CC0 / CC BY / CC BY-NC… (per photo) |
| Side panel articles | Wikipedia / Wikispecies API (TextExtracts), cleaned text (tag allow-list) | CC BY-SA |

MegaDetector runs in PyTorch, like OWLv2 and BioCLIP, hence on the same GPU whatever it is. Its official weights
are downloaded on first launch from where upstream publishes them ([Zenodo](https://zenodo.org/records/15398270),
CC-BY 4.0, checksum verified), as PytorchWildlife does; the model code (RT-DETRv2, Apache 2.0) is vendored in
`src/taxonero/rtdetr` (see `NOTICE`), without PytorchWildlife or ultralytics (AGPL). Same results as the ONNX
version used so far: 112 out of 112 boxes identical on the dev photos, score difference ≤ 0.00002. An ONNX file
given by `DETECTOR_PATH` is still used if it exists.

### Measured results

Test set: 73 French iNaturalist observations (research grade, CC0/CC-BY), all groups, deliberately
hard (rare species, spiders, lichens). Small sample: take the figures as orders of magnitude.

Full pipeline (MegaDetector + OWLv2, BioCLIP 2, GBIF geographic filter, de-duplication), measured with
`dev/benchmark_e2e.py`:

| Measure | Result |
|---|---|
| Exact species (final decision) | **53/73 (73 %)**, OWLv2 on every photo; when the tool cannot settle the species, it suggests the genus or family |
| Per group | birds 16/18, mammals 10/13, insects 6/8, amphibians 6/8, fungi and lichens 8/10, plants 4/6, reptiles 2/4, fish 1/3, spiders 0/3 |
| Automatic confirmation (default rule) | 39 detections confirmed without review, 35 correct; 3 of the 4 misses are debatable labels (harrier *hudsonius*/*cyaneus*, fungus parasitic on a liverwort, fox subspecies), the 4th a salamander taken for a holly |
| Effect of the geographic filter | +15 points of correct species (BioCLIP alone: 59 % top-1, 74 % with GBIF) |
| Speed | ~6 s/photo on a laptop CPU (OWLv2 as fallback), ~19 s with OWLv2 on every photo; well under a second expected on GPU |

## Installation

Requirements: Immich v3 (tested on 3.3.5), and Docker or the desktop edition (no Docker). A GPU is recommended
(designed for an RTX 3060 12 GB); it also runs on the CPU, much more slowly.

Three ways to install it: a ready-made **Docker image** (Linux server, NAS), **TrueNAS SCALE** (same image), or the
**desktop edition** (Windows, macOS, Linux: a small launcher that installs the rest).

### Test status

A personal project, tested with the hardware at hand: the rest is planned in the code but has never run.
Feedback welcome (GitHub issue, with what **Settings > AI models** shows).

| Install | Hardware | Status |
|---|---|---|
| Docker, Linux x86 | CPU | tested (Fedora, Podman) |
| Docker, Linux x86 | NVIDIA, `cuda13` image | in use on TrueNAS SCALE with an RTX 3060 |
| Docker, Linux x86 | NVIDIA, `cuda12` image | untested |
| Docker, Linux x86 | AMD, `rocm` image | untested |
| Docker, Linux x86 | Intel Arc / Core Ultra, `xpu` image | untested; on Iris Xe (not supported by PyTorch), CPU fallback checked |
| Docker, Linux ARM (Raspberry Pi 5, ARM NAS) | CPU | untested (image built under emulation) |
| Docker Desktop, macOS or Windows | CPU | untested |
| Desktop, Linux x86 | CPU | tested (Fedora); .deb, .rpm and AppImage: install and first launch checked |
| Desktop, Linux | NVIDIA, AMD, Intel | untested |
| Desktop, Windows x86 | NVIDIA (RTX 4070) | installer and identification checked |
| Desktop, Windows | CPU, AMD, Intel; ARM | untested |
| Desktop, macOS (Apple Silicon) | Apple GPU (Metal) | untested |

A GPU that reports itself available but crashes on first use blocks nothing: at startup, a small test
computation runs in a separate process, and if it fails the analysis runs on the CPU (reason shown in
**Settings > AI models**). On a Mac, an operation the Apple GPU cannot do runs on the CPU
(`PYTORCH_ENABLE_MPS_FALLBACK`).

### Compute hardware

By default the AI runs on the GPU if a usable one is found, otherwise on the CPU; change it in
**Settings > Analysis > Compute** (GPU or CPU, models reloaded at the next analysis) or with `DEVICE` (`auto`, `cpu`,
or precisely `cuda`, `xpu`, `mps`). A requested but missing GPU falls back to the CPU instead of stopping
the analysis.

**OWLv2** (plants, fungi, insects) is the slowest model on the CPU. **Settings > Analysis > Compute** (or
`OPEN_DETECTOR_MODE`) runs it on every photo (`always`) or only when MegaDetector finds no animal
(`fallback`). With `auto` (default): `always` with a GPU, `fallback` on the CPU. Measured on a laptop CPU
(73 photos from `dev/photos`, `dev/benchmark_e2e.py`):

| OWLv2 | Time per photo | Correct species | Automatic confirmation |
|---|---|---|---|
| `always` | 18.6 s | 72.6 % | 39 detections, 89.7 % correct |
| `fallback` | 6.3 s | 71.2 % (one plant fewer) | identical |

On 12 photos of domestic animals, `fallback` recognizes 5 as domestic instead of 7: two more are left to
review. int8 quantization was also measured and dropped: 5.5 points of accuracy lost for a negligible
time gain on the whole photo.

Memory: BioCLIP's text embeddings (2.7 GB) are memory-mapped, so the system can reclaim them under memory
pressure, and the text half of the model, useless once those embeddings are computed, is freed. Results
are bit-for-bit identical. `MEM_LIMIT` (8 GB by default) caps the container.

On GPU, photos are analyzed in batches (automatic from the GPU memory, 8 for 12 GB and 16 for 24 GB, halved on
its own if the memory runs out; or set with `GPU_BATCH` or **Settings > Analysis > Compute**). MegaDetector and
OWLv2 (half as many at once) process each batch in one pass, and all their crops are identified in a single
BioCLIP pass; the number of previews downloaded ahead follows the batch size, and
the previews of the next photos are downloaded while the models work. Results are identical to
photo-by-photo analysis; on the CPU, batching brings nothing and photos go one at a time.

Models leave GPU memory after 5 minutes without analysis and are reloaded for the next one (`MODELS_IDLE_MINUTES`
or **Settings > Analysis > Compute**; 0: always kept). The process keeps its CUDA context, so a small part of the
memory stays in use until it stops.

Results are the same on GPU and CPU: everything is computed in full float32. Recent NVIDIA GPUs use TF32
(reduced precision) by default in cuDNN and in ONNX Runtime's CUDA; the tool turns it off.
`dev/compare_devices.py` compares both on a set of photos (run it on the machine with the GPU). The
**Settings > AI models** page shows what actually runs (GPU, memory, engine of each model, measured speed).

The machine's hardware is set with `GPU=` in `.env`, which `./detect-gpu.sh` fills in by itself: the
variable selects both the image (`ghcr.io/adn-dan/taxonero:latest-<GPU>`) and the hardware access
(`hwaccel.yml`, same idea as Immich's `hwaccel.ml.yml`). The same `docker compose up -d` therefore works on every
machine.

| `GPU=` | Hardware | Image size | Notes |
|---|---|---|---|
| `cuda13` | NVIDIA, driver ≥ 580 | ~7 GB | torch 2.14 (CUDA 13) |
| `cuda12` | NVIDIA, driver ≥ 525 | ~7 GB (estimated) | torch 2.11 (CUDA 12.8) |
| `rocm` | AMD (ROCm 7.2) | over 15 GB | untested |
| `xpu` | Intel Arc (A and B cards), Core Ultra | ~8 GB | untested; Intel GPU driver included; Iris Xe, UHD and older: not supported by PyTorch |
| `cpu` (default) | no GPU, x86 and ARM | ~2 GB | |

An NVIDIA GPU needs the NVIDIA Container Toolkit on the host (the script warns if it seems missing). AMD and
Intel: the container does not run as root, it gets the group that owns `/dev/dri/renderD*` (`RENDER_GID`,
written by the script).

All three models run in PyTorch: the chosen GPU serves them all (no ONNX Runtime version to match anymore).

### Docker (any Linux machine, Unraid, Synology, Portainer…)

1. Get the repository (`git clone`, or the archive of the latest release), then `./detect-gpu.sh`: it creates
   `.env` from `.env.example` and writes the detected hardware (`GPU=`). Set `DASHBOARD_PASSWORD` and `TZ` in `.env`.
2. `docker compose up -d`: downloads the image for this hardware, then starts it. On the first analysis, ~5 GB
   of models are downloaded into `./data`.
3. Open `http://<server>:8765` (user `nature`). On first start, the dashboard opens **Settings > Photo source > Immich**:
   the Immich address as seen from the container, and an API key. The address is the machine's IP and
   Immich's published port (`http://192.168.1.10:2283`), or `http://immich-server:2283` after joining
   Immich's Docker network (commented lines at the end of `docker-compose.yml`).
   Create the key in Immich, **Account Settings > API Keys > New API Key**, ticking only `asset.read`,
   `asset.update`, `asset.view`, `server.about`, `tag.asset`, `tag.create`, `tag.delete`, `tag.read`
   (the list can be copied from the dashboard). Optional: `asset.download` (read only) to identify small
   subjects on the full-resolution original rather than on the preview (1440 px on the short side), and
   to zoom in at full resolution in the viewer.
   **Test** tells you what is wrong (address unreachable, key rejected, permission missing or extra);
   **Save** only stores a connection that works. No Immich account or password: the tool only sees the
   photos of the account that created the key. Alternative: `IMMICH_URL` and `IMMICH_API_KEY` in `.env`
   (they take precedence and are then locked in the dashboard).
4. **Settings > AI models** shows what actually runs (GPU, engines). Review a few dozen detections.
5. When the results look right: `DRY_RUN=false`, then `docker compose up -d`.

Update: `docker compose pull && docker compose up -d` (`VERSION=0.1.0` in `.env` pins a version instead of
following `latest`).

Build the image yourself (modified code, no access to GHCR): `docker compose -f docker-compose.yml -f
docker-compose.build.yml up -d --build` (15 to 30 min the first time).

Portainer (or Unraid, Synology): a *Stack* from the repository with `docker-compose.yml` and `hwaccel.yml`, and the
variables of `.env.example`. Podman: `podman compose` works the same; for an NVIDIA GPU, Podman uses CDI
(`devices: [nvidia.com/gpu=all]` instead of the `deploy` block of `hwaccel.yml`). Docker Desktop on macOS: a
container cannot reach the Apple GPU, so the analysis runs on the CPU (the Apple GPU is used by the desktop
edition).

### TrueNAS SCALE

The app is installed with a ready-made YAML file that uses the GHCR image.

1. GPU: **Apps > Configuration > Settings**, tick *Install NVIDIA Drivers* (and do not isolate the card for
   a VM).
2. Create the data folder owned by the apps user, in the TrueNAS shell (**System > Shell**):
   `sudo mkdir -p /mnt/POOL/apps/taxonero/data && sudo chown 568:568 /mnt/POOL/apps/taxonero/data`.
3. NVIDIA driver (`nvidia-smi`): from 580, image `latest-cuda13`; otherwise `latest-cuda12`; without a GPU,
   `latest-cpu` and remove the `deploy` block.
4. **Apps > Discover Apps > ⋮ > Install via YAML**: paste [`deploy/truenas/taxonero.yaml`](deploy/truenas/taxonero.yaml),
   after setting the image, the pool and the password.
5. Open `http://<nas>:8765`, then **Settings > Photo source > Immich**: the NAS address and the Immich app port
   (`http://192.168.1.10:30041` by default) and an API key (step 3 of the Docker install). TrueNAS apps
   do not share a network, hence the NAS address rather than a container name.
6. **Settings > AI models** must show the GPU, and MegaDetector on "NVIDIA GPU (CUDA)".

Update: **Apps > taxonero > Edit**, then *Save* without changing anything: the YAML asks for the image to be
pulled again at each deployment (`pull_policy: always`), hence the latest `latest`. To build the image
on the NAS instead: copy the sources, `sudo sh deploy/truenas/build.sh /mnt/POOL/apps/taxonero/data`, then in
the YAML `image: taxonero:<variant>` and `pull_policy: never`.

### Desktop edition (Windows, macOS, Linux; x86 and ARM)

The same program, on your computer, without Docker or a server. Like ComfyUI Desktop or Invoke, the app is a small
launcher (~35 MB: uv, the program, ExifTool), the same for every machine of a given system; on first launch it
installs, in the user folder, the PyTorch build matching the detected GPU (a few minutes, ~2 to 5 GB depending on
the GPU, progress shown in a small window), then the AI models (~5 GB, in the same window). One executable per GPU would have weighed several GB
(CUDA PyTorch) and exceeded the 2 GB per-file limit of GitHub releases.

| Detected GPU | Windows | Linux | PyTorch installed |
|---|---|---|---|
| NVIDIA, driver ≥ 580 | ✓ | ✓ (x86 and ARM) | 2.14, CUDA 13 |
| NVIDIA, driver ≥ 525 | ✓ | ✓ (x86) | 2.11, CUDA 12.8 |
| AMD Radeon | AMD preview: RX 7000/9000, driver 26.1.1 | ✓ ROCm 7.2 | Windows: AMD's 2.9.1; Linux: 2.14 |
| Intel Arc, Core Ultra | ✓ | ✓ (Arc or `xe` driver) | 2.14, XPU |
| Apple Silicon | | | macOS: 2.14, Metal |
| none | CPU | CPU | 2.14 CPU |

`TAXONERO_GPU=cpu|cu130|cu128|rocm|xpu|mps|amd-win` forces a variant; `taxonero --reinstall` redoes the
installation (other card, repair). If the GPU installation fails, the launcher falls back to the CPU. Intel Macs
are not supported (PyTorch stopped there in 2024, version 2.2).

Download: the [**Releases**](https://github.com/adn-dan/taxonero/releases) page of the repository.

| System | File | Install |
|---|---|---|
| Windows (x64, ARM) | `Taxonero-<version>-windows-<arch>-setup.exe` | installer: Start menu, uninstall from Windows settings, no admin rights needed |
| | `…-portable.zip` | `taxonero.exe`, nothing to install |
| macOS (Apple Silicon) | `Taxonero-<version>-macos-arm64.dmg` | drag Taxonero into Applications |
| Debian, Ubuntu, Mint | `taxonero_<version>_<arch>.deb` | `sudo apt install ./taxonero_….deb` |
| Fedora, openSUSE | `taxonero-<version>-1.<arch>.rpm` | `sudo dnf install ./taxonero-….rpm` |
| any Linux | `Taxonero-<version>-<arch>.AppImage` | `chmod +x`, then run it |

The Linux packages were checked (install and first launch on Debian 13, Fedora 44 and Ubuntu 24.04); the Windows
installer on Windows 11 x86; the macOS app is built automatically but untested (see "Test status"). Uninstalling leaves the data
folder below (models, PyTorch: several GB); delete it by hand. Requirements depending on the GPU:

- **NVIDIA**: the usual NVIDIA driver is enough (CUDA comes with PyTorch).
- **AMD on Linux**: the kernel's `amdgpu` driver, and the user in the `render` and `video` groups.
  **AMD on Windows**: AMD driver 26.1.1 or later, Radeon RX 7000 or 9000.
- **Intel on Linux**: Intel's compute driver (Fedora: `intel-compute-runtime`; Ubuntu:
  `intel-opencl-icd libze-intel-gpu1`; otherwise the [compute-runtime](https://github.com/intel/compute-runtime/releases)
  packages). Iris Xe, UHD and older: CPU, PyTorch does not support them.
- **Mac**: Apple Silicon (M1 and later), nothing to install.

- **No account or password**: the dashboard only listens on the computer (127.0.0.1) and only accepts the app's
  window, thanks to a random token created at each launch (like Jupyter); a web page or another account on the
  machine cannot drive it.
- **Data** in the user folder: `%LOCALAPPDATA%\taxonero` (Windows),
  `~/Library/Application Support/taxonero` (macOS), `~/.local/share/taxonero` (Linux).
- **Source** (Immich or a folder, with "Browse…") and **writing results** are set in **Settings > Photo source**.
- Everything is shown in an application window (pywebview: WebView2 on Windows, WebKit on macOS, Qt on Linux,
  installed with the app, ~150 MB), never in the browser.

From source: `pip install '.[desktop,cpu]'` then `taxonero desktop`. Build the launcher:
`python packaging/prepare.py && pyinstaller packaging/taxonero.spec` (done by `.github/workflows/desktop.yml` for
the five platforms, with the packages above: `packaging/linux-packages.sh`, `packaging/taxonero.iss`). Unsigned
applications: Windows (SmartScreen) shows "Windows protected your PC" (More info > Run anyway); macOS refuses to open
it the first time (System Settings > Privacy & Security > Open Anyway).

## Configuration

### Photo source: Immich, a folder or a photo application

By default, photos come from Immich. With `SOURCE=local`, they come from a folder (and its subfolders), mounted
in the container at `PHOTOS_DIR` (default `/photos`); Immich is then not used. Results are written to an XMP
file next to each photo, never into the photo itself: hierarchical keywords (`Nature|Aves|Paridae|…`,
`lr:HierarchicalSubject`, plus the last level as a plain keyword) and description. These files are read by
digiKam, darktable, Lightroom Classic (for RAW files; for a JPEG, Lightroom only reads metadata embedded in the
file), Adobe Bridge and Immich (external libraries). File name: the existing one, otherwise `photo.xmp` for a RAW
and `photo.jpg.xmp` for the rest. Formats: JPEG, PNG, WebP, TIFF, HEIC and RAW (camera-embedded preview). No
videos and no face recognition (`SKIP_PEOPLE` has no effect). The folder must be writable by the container
user; with `DRY_RUN=true`, nothing is written.

```yaml
    volumes:
      - ./data:/data
      - /path/to/my/photos:/photos   # SOURCE=local
```

In the **desktop edition**, the source can also be a photo application installed on the computer (Settings >
Photo source). Results still go to XMP files, under the name that application reads; its library is never
changed, and the settings it needs are checked (warning in Settings otherwise).

| Source | Photos | XMP file written | To set in the application |
|---|---|---|---|
| `darktable` | those in its library (`library.db`, `CATALOG_PATH`) | `photo.nef.xmp`, and `photo_01.nef.xmp`… for each duplicate | Preferences > storage: "look for updated XMP files on startup"; it then offers to reload changed XMP files at startup |
| `lightroom` (Windows, macOS) | those in the `.lrcat` catalog (`CATALOG_PATH`), read with Lightroom closed | `photo.xmp` next to RAW files | Catalog Settings: "Automatically write changes into XMP", then Metadata > "Read Metadata from Files" |
| `rawtherapee` | a folder (`PHOTOS_DIR`) | `photo.xmp` or `photo.nef.xmp`, per its setting | Preferences > Metadata: "XMP sidecar synchronization" set to "read" (keywords and description used on export) |
| `rapidraw` | a folder (`PHOTOS_DIR`) | `photo.xmp` | XMP sync on (Settings) |

Known limitations:
- Lightroom Classic only reads metadata of a JPEG, TIFF, HEIC or DNG from the file itself, which is never
  changed here: only RAW files get the keywords in Lightroom. The Lightroom connector was only tested on a
  rebuilt catalog (public `.lrcat` schema), not with Lightroom itself.
- RapidRAW adds keywords from XMP files to its own and never removes any: a keyword removed here (rejected
  detection) must be removed in RapidRAW too. It was not tested with RapidRAW itself (format checked in its
  source code).
- darktable rewrites its XMP files from its database: editing a photo before accepting the XMP reload at
  startup erases the keywords written here (they are written again by the daily synchronization).

### Options

Every option is documented in [`.env.example`](.env.example). Part of them can also be set from the
dashboard's **Settings** page (candidate filters, domestic animals, automatic confirmation, videos,
description, time window, frequency): those settings are stored in the database and take precedence over
the environment. Sensitive settings (`DRY_RUN`, dashboard password, `NAMES_LANG`) stay in the environment.
The main ones:

- `EXCLUDE_EXTINCT` (default `true`): drops the extinct species BioCLIP sometimes suggests, mostly on
  photos without GPS (fossil horses and dogs). Criterion: more than 5 fossils per field observation in
  GBIF; GBIF's "extinct" flag is not used, it is wrong for some living species (the stone marten, for
  example).
- `HIDE_ABSENT` (default `false`): drops species never observed in the area.
- `ANALYZE_VIDEOS` (default `true`): analyzes the key frame of videos.
- `SKIP_PEOPLE` (default `true`): photos where Immich recognized at least one person are not analyzed
  (portraits, family photos): much less computing and fewer false detections.
- `SKIP_RAW_TWINS` (default `true`): a RAW file paired with a JPG (or HEIC) of the same shot, same file name
  and same capture time, is not analyzed: it gets the JPG's tags. With `false`, the RAW is analyzed too.
- `FULL_IMAGE_FALLBACK` (default `false`): when no detector finds a subject, classify the whole photo
  anyway. On the dev benchmark it only helped for an alga and a lichen; on a family library it mostly adds
  false positives (full-frame boxes on landscapes, indoor shots) and GBIF requests. The Identify page always
  does it.
- `DETECTION` (default `auto`): `manual` turns off all automatic detection; you draw the boxes yourself in the
  viewer and each one is identified right away (nothing else shows up). Switching to manual removes automatic
  detections still awaiting review; reviewed ones stay.
- `DETECT_GROUPS` (default `animal,plant,fungus`): groups to look for, among `animal`, `plant`, `fungus`, `microbe` (bacteria, viruses and microscopic protists: almost always wrong on a photo, so off by default). Without `animal`, MegaDetector does not
  run; a subject identified in a group not looked for is dropped before the GBIF requests. Unticking a group
  removes its detections awaiting review; ticking it again does not re-analyze photos already processed
  (`taxonero reanalyze all`, reviews kept). Both are set in **Settings > Analysis > Detection**.
- `GBIF_LOCAL` (default `true`): local copy of the GBIF taxonomy (GBIF Backbone Taxonomy, same version as the
  API, hence the same identifiers). On first start the archive (~0.9 GB) is downloaded in the background,
  then converted into `data/gbif-backbone.sqlite` (~1.5 GB, about ten minutes; keep ~2.5 GB free during the
  conversion). From then on, name matching and taxon records (rank, lineage) no longer go over the network:
  on the benchmark with an empty cache, 34 requests per photo instead of 55, twice as fast. Still online:
  presence around the point (one request per 1° area for all species), fossils (one world list of the species
  with fossils, once a year), common names and IUCN status (once per taxon), maps.
  The status is shown in **Settings > AI models**. If the API moves to a new taxonomy version, the copy is
  rebuilt at the next start.
- `ANALYSIS_WINDOW`: time window for analysis (e.g. `01:00-07:00`), to leave the GPU to Immich's machine
  learning during the day. Within the window, the analysis keeps going until the queue is empty (at most
  `ANALYSIS_MAX_MINUTES` per cycle, 30 by default, so tags get written in between). A photo whose
  analysis fails 3 times is set aside ("Failed" filter, with a button to retry).
- `AUTO_CONFIRM`: `rule` (default), `calibrated`, `fixed` or `off`. In `rule` mode, a detection is
  confirmed automatically when all three conditions hold: confidence ≥ 0.85, species observed nearby
  (green badge) and no other candidate above 5 %. After 100 reviews, the threshold calibrated on your
  reviews (98 % target precision, Wilson lower bound) replaces it if it is stricter. On top of that, a
  group of living things where the model is often confidently wrong is never confirmed automatically: by
  default spiders and fish (measured as weak on the test set), then, from 10 reviews of a group onwards,
  based on your measured precision for that group. The list is shown in **Settings > Automatic
  confirmation**. Automatic confirmations stay visible and correctable (Photos page, "Auto-confirmed"
  filter).
- `AUTO_CONFIRM_AUDIT` (default 0.05): share of detections that would have been confirmed automatically,
  kept for review at random ("Audit" filter). Without them, your reviews only cover doubtful cases and the
  calibration never sees the high-confidence zone; the summary is shown in the statistics.
- `NAMES_LANG` (tags, description) and `NAMES_LANGS` (names shown in the dashboard).
- `PUID` / `PGID`: container user, owner of `./data` (TrueNAS SCALE: often 568).

## Command line

```
taxonero check                 compatibility with the Immich server
taxonero scan                  new or changed photos
taxonero analyze [--limit N]   detection + identification
taxonero sync [--apply]        write to Immich (dry run without --apply)
taxonero uninstall [--apply]   remove from Immich everything the tool wrote
taxonero calibrate             automatic confirmation threshold measured on your reviews
taxonero reanalyze SCOPE       queue again: failed, outdated (other model version), all
                                    (reviewed detections are kept)
taxonero serve                 dashboard + automatic loop (container default command)
```

## Sync

Each cycle only re-reads in Immich the photos whose desired state changed (new confirmation,
correction); a full check, once a day, puts back a tag removed by hand, catches a lost write and spots
photos deleted or moved to the trash.

## Backup

The `data/taxonero.sqlite` database is the only copy of your reviews (Immich only receives a
projection of them). The tool makes a copy every day in `data/backups/` (`taxonero-YYYY-MM-DD.sqlite`,
consistent even during analysis) and keeps the `BACKUP_KEEP` most recent ones (7 by default, 0 to turn
off). Copies leave out the network caches (GBIF and Wikidata answers, range maps) and the Immich API key:
a few MB. Remember to include `data/backups/` in the server's backup (TrueNAS: dataset snapshots).

Restore: stop the container, copy the backup you want over `data/taxonero.sqlite`, start it again,
and enter the API key again in **Settings > Photo source > Immich**. The caches rebuild themselves; the rest of `data/`
(models) is downloaded again.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[cpu,dev]'
systemctl --user start podman.socket        # or Docker
docker compose -f dev/docker-compose.yml up -d   # throwaway Immich on 127.0.0.1:2284
./dev/bootstrap.sh                          # dev admin + restricted API key -> dev/.env.dev
.venv/bin/python dev/fetch_dataset.py       # iNaturalist test photos with ground truth
PYTHONPATH=src .venv/bin/python dev/screenshots.py   # README screenshots and their credits (docs/screenshots)
.venv/bin/python dev/fetch_series.py "Erithacus rubecula:6" "Salamandra salamandra:4"   # bursts of one species
.venv/bin/python dev/fetch_domestic.py      # domestic animals (always the same observations)
./dev/upload.sh
.venv/bin/playwright install firefox        # browser for the interface tests
.venv/bin/pytest                            # unit, API, integration and interface tests against the dev Immich
```

Interface: `static/index.html` (structure), `static/app.css`, and `static/js/*.js`, plain scripts loaded
in order (no build step): `core.js` (language, navigation, sync), one file per page, then `main.js`, the
only one running code at startup. `tests/test_ui.py` opens every page in Firefox (Playwright), on desktop
and phone sizes: no JavaScript error, no horizontal overflow, Grid and Tree views, side panel,
identification with map, Immich connection.

`dev/benchmark_e2e.py` measures end-to-end accuracy on `dev/photos`; `--owl-mode fallback` compares the
OWLv2 modes (see *Compute hardware*).

Before upgrading Immich in production: start the dev Immich at the new version (`IMMICH_VERSION=vX.Y.Z`)
and run `pytest` again. If everything passes, add the version to `TESTED_VERSIONS` in
`immich/contract.py`.

## Known limitations

- **GBIF outage.** Without its occurrence service, a drawn or reframed box and the Identify page give their candidates
  without the geographic check ("not checked"), and a location you set is saved without reclassifying detections. In
  both cases, re-analyze the photo once GBIF is back. Automatic analysis retries on its own.
- **Limited log.** The Log tab in Settings keeps the last 2000 warnings and errors (in `data/journal-erreurs.jsonl`, even after a
  restart; the Clear button empties the log) and the last 2000 other lines, lost on restart. The full log stays on standard output (`docker logs`).
- **Write race in Immich 3.3.5.** Two back-to-back changes on the same photo (description then tag,
  less than ~0.5 s apart) can lose one: the XMP sidecar write job unlocks properties changed in the
  meantime. The tool spaces its writes 2 s apart per photo, re-reads the actual state and repairs if
  needed, and leaves alone a photo changed by someone else in the last 10 minutes.
- No boxes in Immich's own interface: Immich has no interface extensions, and hijacking face recognition
  would mix species and people. The boxes are in the dashboard.
- MegaDetector is trained on camera traps: it detects mammals and birds well, insects and macro subjects
  less so, hence OWLv2 alongside. OWLv2 sometimes gets the category wrong (alga taken for a lichen, gall
  for a spider), so its kingdom hint is soft. Without any detection, the whole image is classified, and
  the result is only kept above 60 % confidence.
- Overlapping detections pointing to the same taxon are merged. Two different overlapping subjects
  (butterfly on a flower) stay separate.
- BioCLIP is biased towards heavily photographed species (unbalanced training set).
- Range maps are densities of GBIF observations, not official ranges (IUCN): they also reflect
  observation effort, and a few isolated observations (mislabeled captive animals) appear very faintly.
- Domestic animals: a purely domestic species (*Felis catus*, *Equus caballus*…) is dropped outright; for
  ambiguous families (dog/wolf…), OWLv2 must see "a dog" rather than "a wolf" on the subject. This is
  imperfect (on the dev set, about half of the dogs get no hint): those stay to review, and a species that
  can be confused (*Canis lupus*, *Felis silvestris*, *Sus scrofa*…) is never confirmed automatically.
- OpenStreetMap base map loaded from the Internet (no local tiles); the rest of the interface is served
  locally.
- Full-resolution original: only for formats Pillow can read (JPEG, PNG, WebP, TIFF); RAW and HEIC are
  identified on the preview.
- Videos: a single frame per video (Immich's key frame); an animal that only shows up later in the video
  is missed.
- Hardware: only the CPU (Linux) and an NVIDIA GPU (RTX 3060, `cuda13` image) have been tried; see "Test status".
  On first start, check **Settings > AI models** (expected GPU, reason for the choice). GPU batching is checked on
  the CPU (same results) but not yet measured on a GPU. torch is pinned per variant in the Dockerfile, other
  libraries in `constraints.txt`.
- The `rocm` image weighs over 15 GB (PyTorch for AMD); its automatic build on GitHub may fail for lack of disk
  space, in which case build it yourself (`docker-compose.build.yml`).

## Credits

The screenshots show photos by [iNaturalist](https://www.inaturalist.org) observers, under their own licenses (CC0,
CC BY and, for some reference photos, CC BY-SA, CC BY-NC or CC BY-NC-SA). The code license does not cover them.
Authors, licenses and sources: [docs/screenshots/CREDITS.md](docs/screenshots/CREDITS.md).

## License

[AGPL-3.0](LICENSE). Any fork or modified version must stay open source, including when it is only
offered as an online service (the dashboard is a web application).

Models and data keep their own licenses: MegaDetector v6 RT-DETR (Apache 2.0), BioCLIP 2 (MIT), GBIF data
(CC0 / CC-BY depending on the dataset), Wikidata (CC0).
