## Resolume Simple

A deliberately small Resolume Arena module.

**In Resolume** (Preferences):

- **Webserver**: enabled; note the port (for example 8090).
- **OSC**: OSC Input enabled; note the port (for example 7000). It is only needed for the *Send OSC* action.

**Connection settings:** Resolume host (IP or name, without `http://` or a port), Webserver port, OSC input port.

**Actions:**

- **Connect column by name**: finds the column by its name inside the given layer group and connects it. The default layer group is `2`; `0` means the composition's own columns.
- **Connect column by number**: connects column N of the given layer group (no name lookup). Use it when the columns have no useful names or Arena was not reachable when the button was set up.
- **Select deck by name**: finds the deck by its name and selects it. Variables work, for example `$(AbleSet:activeSongName)`.
- **Send OSC**: sends one OSC message (path plus an optional integer, float or string) over UDP. Arena never confirms OSC, so a wrong port shows up only as "nothing happens". Every send is logged at debug level with host and port.

**Name matching:** an exact match wins; otherwise upper/lower case is ignored. A `#` in a name also matches the number Arena shows for it (for example "Kosik #" in column 10 also answers to "Kosik 10"). Arena confirms every connect or select. A failure (unknown name, Arena not answering) is written to the log and nothing else happens. If you press quickly several times, the last press always wins.

**Status:** once connected, it turns red only when Arena's webserver misses two health checks in a row, so a short stall while Arena is busy does not make it flap. At startup or after a settings change the first miss already shows red. Changes are logged only when the state changes.

**Timeouts:** each request to Arena waits up to 5 s. A press by name makes two small requests (check the name, then connect), and a press that comes right behind a stalled one waits for it first, so in a long stall a press can land several seconds late. If a stall is longer than that, the press fails and the log says so. The last press always wins. The module never opens a WebSocket and never downloads the whole composition, so it stays fast with big compositions.
