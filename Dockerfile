FROM python:2.7
MAINTAINER "Julien Ripault"

RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app

ENV HTTP_PROXY=http://proxy.priv.atos.fr:3128 http_proxy=http://proxy.priv.atos.fr:3128 https_proxy=http://proxy.priv.atos.fr:3128 HTTPS_PROXY=http://proxy.priv.atos.fr:3128

COPY requirements.txt /usr/src/app/
RUN pip install --no-cache-dir -r requirements.txt

COPY dockerdns.py /usr/src/app/


ENTRYPOINT ["python","/usr/src/app/dockerdns.py","-r proxy.priv.atos.fr:10.24.219.17"]