# A dashboard server in a container. Anything that can make an HTTP request
# (other containers, other machines) feeds it with POST /update.
FROM python:3.13-slim

RUN pip install --no-cache-dir "sightglass[web]"

EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/values')"

# Listen on all interfaces inside the container; publish the port with -p.
ENTRYPOINT ["sightglass", "--host", "0.0.0.0", "--port", "8080"]
CMD ["--title", "Dashboard"]
