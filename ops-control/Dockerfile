FROM docker:27-cli
RUN apk add --no-cache python3 git
COPY --from=docker/compose-bin:v2.39.2 /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
COPY control.py /opt/control.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOCKER_CONFIG=/tmp/docker
EXPOSE 8765
CMD ["python3", "/opt/control.py"]
