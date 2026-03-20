# Start with alpine to get rambler binary
FROM zhaowde/rambler:5.4.0-rag2 as rambler-source

# Use Ubuntu as final base for glibc compatibility
FROM ubuntu:22.04

# Install netcat for connectivity checking and postgresql-client for psql
RUN apt-get update && apt-get install -y netcat-openbsd postgresql-client && rm -rf /var/lib/apt/lists/*

# Copy rambler binary from the source image
COPY --from=rambler-source /rambler /usr/local/bin/rambler
RUN chmod +x /usr/local/bin/rambler

# Copy migration files
COPY ddl /migrations

# Set the working directory for rambler
WORKDIR /migrations

# Set default environment variables that can be overridden
ENV RAMBLER_DRIVER=postgresql
ENV RAMBLER_PROTOCOL=tcp  
ENV RAMBLER_DIRECTORY=/migrations
ENV RAMBLER_TABLE=migrations
ENV RAMBLER_SSLMODE=require

COPY migrate.sh .
RUN chmod +x ./migrate.sh
USER 1000
ENTRYPOINT ["./migrate.sh"]
