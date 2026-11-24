FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
WORKDIR /srv
COPY service.py version.txt ./
RUN chmod -R a+rX,go-w /srv
USER 10001:10001
EXPOSE 8080
CMD ["python", "service.py"]
