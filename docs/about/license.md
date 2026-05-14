# License

Beetroot is released under the MIT License.

```
MIT License

Copyright (c) Xiddoc

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Third-party components

The dependencies Beetroot packages or orchestrates carry their own licenses:

| Component | License |
|-----------|---------|
| [redroid](https://github.com/remote-android/redroid-doc) | Apache 2.0 |
| [Magisk](https://github.com/topjohnwu/Magisk) | GPL-3.0 |
| [Frida](https://frida.re/) | wxWindows Library Licence 3.1 |
| [Shamiko](https://github.com/LSPosed/LSPosed.github.io) | — (closed-source release) |
| [LiteGapps](https://litegapps.github.io/) | Various (Google proprietary) |

Beetroot's own code (the `beetroot` CLI and the scripts in `docker/` and `scripts/`) is MIT-licensed. The Docker image you build with `./scripts/setup.sh` bundles third-party components under their respective licenses.
