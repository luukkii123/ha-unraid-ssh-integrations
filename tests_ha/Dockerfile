FROM python:3.14-slim-bookworm AS tests
ARG HA_VERSION=2026.9.2
ARG TEST_PLUGIN=0.13.365
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libffi-dev libssl-dev libpcap0.8 libturbojpeg0 php-cli && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir homeassistant==${HA_VERSION} pytest-homeassistant-custom-component==${TEST_PLUGIN} asyncssh==2.24.0 && pip check
WORKDIR /repo

FROM tests AS frontend
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN python -c 'import json,pathlib,subprocess,sys,homeassistant; root=pathlib.Path(homeassistant.__file__).parent/"components"; requirements=set(); [requirements.update(json.loads((root/domain/"manifest.json").read_text()).get("requirements",[])) for domain in ("frontend","ffmpeg","conversation","assist_pipeline","media_source","image_upload","tts","camera","radio_frequency","infrared")]; subprocess.run([sys.executable,"-m","pip","install","--no-cache-dir",*sorted(requirements)],check=True)'
RUN pip check
