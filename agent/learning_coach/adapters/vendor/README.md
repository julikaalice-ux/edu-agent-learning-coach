# SVG.js

Pinned release: `@svgdotjs/svg.js@3.2.0` (MIT).

- Source: https://cdn.jsdelivr.net/npm/@svgdotjs/svg.js@3.2.0/dist/svg.min.js
- License: https://cdn.jsdelivr.net/npm/@svgdotjs/svg.js@3.2.0/LICENSE.txt
- Bundle SHA-256: `0490b8cf4b27f75ceddf1ae462d1142eb3b86f9600e7251175b905c6d1c9057c`

The unmodified bundle and license are checked in. `scenes.py` verifies the
digest and embeds the library into each artifact; it removes only the source-map
reference when embedding. Learners never need a CDN connection or API key.
Updates must change the pinned source, digest, license and browser verification
together. This dependency renders shapes; the application owns the bounded
drawing primitives and does not execute model-generated JavaScript.
