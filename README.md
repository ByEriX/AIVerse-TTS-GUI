# Introduction
This is a script to use the ElevenLabs API to easily
generate TTS message MP3s from a Text-File or via 
copy-pasted text. It includes the following features:

- Character counter for your inputs
- Input is split into multiple chunks with up to 2500 characters
- API-Key management
- Automatic key state reset when quota refills
- Consistent naming for each audio segment

By default, if nothing else is specified, the script will
create an outputs folder in this directory and save
your audio files to that Folder.

On initial launch, the script will create a config file 
in which you can adjust some settings.

# Installation

To use this, you need:

- Python
- requests package

After installing python, you can use

`pip install -r requirements.txt`

from the terminal to automatically install requests.

Alternatively, you can also install requests manually with:

`pip install requests`

# Policy & Responsible Use

This tool is provided for legitimate use only. You must only use API keys that you own and that are authorized for the intended service plan. Do not use this program to circumvent or evade quota, rate limits, or billing restrictions imposed by ElevenLabs or any other provider. Attempting to chain or combine multiple free keys to avoid quotas is a violation of ElevenLabs’ Terms of Service and may result in account suspension, revoked API access, disabled free plan, or other consequences.

If you need higher a quota, upgrade your ElevenLabs plan at https://elevenlabs.io/pricing

Do not ruin the free plan for the rest of us.
