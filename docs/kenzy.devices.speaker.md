# kenzy.devices.Speaker

## Parameters
| Parameter           | Type    | Default | Description |
| :------------------ | :------ | :------ | :---------- |
| useTempFile         | bool    | True    | Pass test through file at runtime. |
| speakerExecFormat   | str     | festival --tts {FILENAME} | TTS shell command. |
| nickname            | str     | None    | (Optional) Nickname of the device |

For the __speakerExecFormat__ you can use {FILENAME} to represent a temp file generated at runtime and {TEXT} for in-line text replacement.

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)