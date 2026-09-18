FROM python:3.12-slim
WORKDIR /sdk
COPY tools/ /sdk/tools/
COPY spec/ /sdk/spec/
ENV PYTHONPATH=/sdk/tools
ENTRYPOINT ["python", "-m", "fake_tunnel_pair", "--host", "0.0.0.0", "--port-base", "8471", "--fixed-tokens", "--no-stdin-watch"]
CMD ["--scenario", "happy", "--longpoll-ms", "1000"]
