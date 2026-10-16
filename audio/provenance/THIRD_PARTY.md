# sfizz 1.2.3 dependency notice inventory

Primary pinned source inspection completed 2026-10-15. `third-party/index.json` links each retrieved license to its exact sfizz tag or pinned Git submodule commit. License texts are preserved in `third-party/`; `sfizz-tree.json`, `.gitmodules`, and `cmake-SfizzDeps.cmake` preserve the discovery basis. This is a source dependency inventory, not a binary-provenance attestation or legal opinion.

| Component | Observed terms | Evidence / applicability |
| --- | --- | --- |
| sfizz | BSD-2-Clause | Root LICENSE; runtime library |
| Abseil | Apache-2.0 | Pinned submodule LICENSE; linked static libraries included in release |
| atomic_queue, cxxopts | MIT | Pinned sfizz subtree LICENSE files |
| fmidi, jsl, st_audiofile | BSD-2-Clause | Their LICENSE.md files; fmidi used by CLI |
| stb_vorbis, dr_libs | MIT or public domain alternative | Preserved dual-license texts; audio decoders |
| libaiff | MIT-style permission notice | `third-party/libaiff-LICENSE`, pinned commit 78864a4a2e769e426be8cfd78ae7f5f72e236c33; original COPYING guess returned 404, corrected to LICENSE |
| WavPack | BSD-style | Pinned submodule COPYING |
| filesystem, SIMDe | MIT | Pinned submodule notices |
| cpuid/platform | BSD-style | Two LICENSE.rst notices |
| hiir | WTFPL v2 | Preserved license.txt |
| kiss_fft, spline | BSD-style | COPYING and LICENSE |
| pugixml, tunings | MIT | LICENSE.md |
| threadpool | zlib SPDX | Header retained; full upstream notice/attribution review still needed |
| Catch2 | BSL-1.0 | Test-only release executable dependency; LICENSE.txt retained |
| cephes | Permission email plus BSD-wrapper text | Pinned LICENSE.txt explicitly says original library had no specified license and quotes permission for inclusion in a BSD package. Do not relabel it as an uncomplicated BSD grant. Review redistribution terms before bundling this third-party binary for others. |

Unresolved: release binary build options/complete embedded code list, generated Faust DSP exception notices, threadpool full attribution, Windows runtime provenance, and cephes permission scope. Optional system libsndfile/JACK/Qt/samplerate declarations in the CMake file do not prove they were linked into this Windows release. The app uses only the DLL, not the tests or CLI. Private local use is demonstrated; a fully reviewed redistributable package is not yet claimed.

Primary source links:

- https://github.com/sfztools/sfizz/blob/1.2.3/cmake/SfizzDeps.cmake
- https://github.com/sfztools/sfizz/blob/1.2.3/.gitmodules
- https://github.com/sfztools/libaiff/blob/78864a4a2e769e426be8cfd78ae7f5f72e236c33/LICENSE
- https://github.com/sfztools/sfizz/blob/1.2.3/external/cephes/LICENSE.txt
- https://github.com/sfztools/sfizz/blob/1.2.3/external/threadpool/ThreadPool.h

Portable setup is `python -m audio.setup_assets`: pinned public HTTPS release URLs, exact byte counts and SHA-256 checks, 8 GiB reserve, bounded extraction, traversal/symlink rejection, temporary staging and atomic directory publication. Existing directories are preserved. No system installation or trust change. Archives in provenance and vendor files are ignored by audio/.gitignore; source licenses remain tracked.
