## bkshading Simple

Steps a camera's ISO, aperture (clona), white balance and tint from Companion, through the **bkshading** camera-shading service on the strih PC (camera-box project). Every step moves the camera exactly like a tap on the bkshading panel.

**Connection settings:** bkshading host (a DNS name such as `strih.lan`, without `http://` or a port), port (default `8770`), and the camera selected at startup (a bkshading id such as `cam1`).

**Actions:**

- **Select camera**: makes a camera (bkshading id, e.g. `cam6`) the one that the steps and the "selected camera" variables act on. Put it on the buttons that pick the camera, e.g. the Grading page's Cam N buttons.
- **Step ISO / aperture / WB / tint**: one step up or down.
  - ISO and aperture move to the next value the camera offers.
  - WB moves 100 K, within 2500–10000 K.
  - Tint moves 1, within −50…50.
  - At the end of a range nothing is sent.
  - The camera is the selected one unless the action names one.

**Variables:**

- For the selected camera: `camera`, `camera_label`, `online` (`on`/`off`), `iso`, `fnumber` (e.g. `f/4.8`), `kelvin` (e.g. `5600K`) and `tint`.
- The same per camera, e.g. `cam6_iso` and `handheld_1_kelvin` (a `-` in an id becomes `_`).
- `service_version`.
- A value is `-` when the camera is not online.

**Feedbacks:**

- **Camera online**: the camera's relay answers and the camera is on USB.
- **Camera is the selected one**.
- **Write not applied by the camera**: bkshading saw that the camera ignored the last write of that parameter. Some lenses ignore aperture writes.

**Fast presses:** presses on one camera are sent one after another, in order, and each one steps from the value the previous one set. Three quick "+" presses are three steps, and none is dropped.

**Errors** are written to the log with the reason; nothing else happens. Typical reasons: the camera is offline, the relay is not reachable, or the camera id is not in bkshading.

**Status:** the connection turns red only after two polls in a row fail. The module polls `GET /api/cameras` every 2 s.
