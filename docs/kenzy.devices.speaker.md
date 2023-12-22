# KENZY: Text-to-Speech (TTS)

## Parameters
| Parameter     | Type    | Default                | Description                                        |
| :------------ | :------ | :--------------------- | :------------------------------------------------- |
| group         | str     | *None*                 | Device membership group                            |
| location      | str     | *None*                 | Location, e.g. "Living Room"                       |
| model_type    | str     | speecht5               | Options are: festival, speecht5                    |
| model_target  | str     | gpu                    | __SpeechT5__ options: gpu, cpu                     |
| speaker       | str     | slt                    | __SpeechT5__ options: [slt, clb, bdl, ksp, rms, jmk](https://huggingface.co/spaces/Matthijs/speecht5-tts-demo) |
| cache_folder  | str     | ~/.kenzy/cache/speech  | Folder for caching spoken phrases                  |

The model type of ```speecht5``` uses the [microsoft/speecht5_tts](https://huggingface.co/microsoft/speecht5_tts) model from [Huggingface.co](https://huggingface.co/).  The festival option calls the external [festival](https://www.cstr.ed.ac.uk/projects/festival/) program

## Example YAML file

See [Service Settings](kenzy.containers.md) for options in the *service* group.
```
type: kenzy.tts

device: 
  location:                 Living Room
  group:                    Primary
  model_type:               speecht5
  model_target:             cpu
  speaker:                  slt
  cache_folder:             ~/.kenzy/cache/speech

service:
  upnp:                     client
  host:                     0.0.0.0
  port:                     9702
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)