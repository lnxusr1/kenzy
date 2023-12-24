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
| offline       | bool    | false                  | Will disable downloading the models |

The model type of ```speecht5``` uses the [microsoft/speecht5_tts](https://huggingface.co/microsoft/speecht5_tts) model from [Huggingface.co](https://huggingface.co/).  The festival option calls the external [festival](https://www.cstr.ed.ac.uk/projects/festival/) program.

Note:  You should consider only setting ```offline``` after you have executed the program at least once so that it fully downloads all model files.  Once they are downloaded you can switch the offline mode on so that it does not try to re-download the models (which enables the program to then run without an Internet connection).

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