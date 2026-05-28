# Reference JSONs — templates de Studio

Estos archivos son el resultado de `getCreativeById(id)` sobre creatives reales
de Studio, usados como **fuente de verdad** para construir el `adTemplate` que
el bot manda a `createCovCreative` para cada tipo de creative.

| Archivo | Tipo | Creative source |
|---|---|---|
| `open_web_template_reference.json` | Standard Video Open Web (COV) | `6a17594842dff3d1bc89eae1` (vertical, *FROM_VE*) |

**No usados aún en código (referencia humana):** sirven para reproducir los
campos del template (`templateShortCode`, `productFamily`, `size`, `metatags`,
`creativeTree`, `manifest`, `HtmlSnippets.compiled`) cuando se construye
`build_*_ad_template()` en `src/studio_api.py`.

**Cómo regenerar (cuando aparezca otro tipo de creative):**
```python
from studio_api import StudioAPIClient
c = StudioAPIClient(jwt_cookie=..., sidecar_path=...)
import json
q = '''query getCreativeById($id: ID!) {
  getCreativeById(id: $id) {
    id name size productFamily templateShortCode status
    country category configuration metatags
    manifest { messages arguments { name type defaultValue } contexts }
    creativeTree
  }
}'''
ref = c._graphql(q, {'id': '<CREATIVE_ID>'})['getCreativeById']
open('docs/references/<TIPO>_template_reference.json', 'w').write(
    json.dumps(ref, indent=2, ensure_ascii=False))
```
