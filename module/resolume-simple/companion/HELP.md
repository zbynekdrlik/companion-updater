## Resolume Simple

A deliberately small Resolume Arena module.

**In Resolume** (Preferences):

- **Webserver**: enabled; note the port (for example 8090).
- **OSC**: OSC Input enabled; note the port (for example 7000). It is only needed for the *Send OSC* action.

**Connection settings:** Resolume host (IP or name), Webserver port, OSC input port.

**Actions:**

- **Connect column by name**: finds the column by its name inside the given layer group and connects it. Layer group `0` means the composition's own columns. The name match is exact first, then case-insensitive. Arena confirms every connect; a failure (unknown name, Arena not answering) is written to the log.
- **Send OSC**: sends one OSC message (path plus an optional integer, float or string) over UDP.

The status turns red when Arena's webserver stops answering. The module never opens a WebSocket and never downloads the whole composition, so it stays fast with big compositions.
