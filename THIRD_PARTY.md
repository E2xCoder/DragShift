# Third-party components

DragShift itself is MIT licensed (see `LICENSE`, © DragShift contributors). This fork adds the components
below. Their licenses are separate from DragShift's, and each one's copyright and license text is kept
here as its authors require.

## AI models (bundled in `dragshift/assets/models/`)

### `realesr-general-x4v3.onnx`: image upscaling and enhancing
- **Model:** Real-ESRGAN "general v3 ×4" (`SRVGGNetCompact`, 1.2 M parameters).
- **Authors:** Xintao Wang et al., <https://github.com/xinntao/Real-ESRGAN>.
- **License:** BSD 3-Clause, © 2021 Xintao Wang. Full text: [`licenses/Real-ESRGAN-BSD-3-Clause.txt`](licenses/Real-ESRGAN-BSD-3-Clause.txt).
- **Where this file comes from:** an ONNX export of the official `realesr-general-x4v3.pth` weights
  (release v0.2.5.0, sha256 `8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292`, as stated by the
  converter), published at <https://huggingface.co/vlad-grig/realesr-general-x4v3-onnx> (commit
  `0e95d93e454cf6b5de9bdf5a1c9e7ec340d38231`). The graph contains only standard ONNX operators.
- **File sha256:** `7b10e80021ec3e0ff35017ae5022791879d93a799b2e201e1010a37510202266` (checked by the app before loading).

### `silueta.onnx`: background removal ("Cutout")
- **Model:** `silueta`, a size-reduced variant of U²-Net, downloaded from the `rembg` project's release page
  (<https://github.com/danielgatis/rembg/releases/download/v0.0.0/silueta.onnx>). Origin of the model:
  <https://github.com/xuebinqin/U-2-Net/issues/295>.
- **U²-Net:** Xuebin Qin et al., <https://github.com/xuebinqin/U-2-Net>, Apache License 2.0. Full text:
  [`licenses/U-2-Net-Apache-2.0.txt`](licenses/U-2-Net-Apache-2.0.txt).
- **rembg:** © 2020 Daniel Gatis, MIT License: [`licenses/rembg-MIT.txt`](licenses/rembg-MIT.txt).
- **Note:** rembg states that model weights carry their own licenses, independent of its MIT license, and the
  `silueta` file does not name a separate license of its own. It is used here on the basis of its U²-Net origin
  (Apache 2.0). Check it yourself before any commercial use.
- **File sha256:** `75da6c8d2f8096ec743d071951be73b4a8bc7b3e51d9a6625d63644f90ffeedb` (checked by the app before loading).

## ffmpeg (video and audio conversion)

- Not stored in this repository. It is installed with the Python package **`imageio-ffmpeg`** (BSD-2-Clause
  wrapper, <https://github.com/imageio/imageio-ffmpeg>) and is included in a built `DragShift.exe`.
- The bundled program is an **ffmpeg 7.1 "essentials" build from gyan.dev**, configured with `--enable-gpl
  --enable-version3`, i.e. it is licensed under the **GNU GPL v3**. ffmpeg's source code and license:
  <https://ffmpeg.org/> and <https://www.gyan.dev/ffmpeg/builds/>.
- **If you give a built `DragShift.exe` to other people**, the GPL applies to that distribution (you must offer
  the corresponding source of the ffmpeg build and keep its license). Using it on your own computer, or sharing
  only this source repository, does not trigger that.

## Python libraries added by this fork

`onnxruntime`, `numpy`, `Markdown-it-py`, `markdownify` (with `beautifulsoup4`), `imageio-ffmpeg`, and the
Windows bindings `winrt-*` for the built-in Windows OCR engine. Each is under its own license; see the
respective package pages. Libraries DragShift already used (PySide6, Pillow, PyMuPDF, python-docx, …) are
unchanged.
