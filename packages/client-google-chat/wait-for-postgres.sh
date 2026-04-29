#!/usr/bin/env bash
#   Use this script to test if a given Postgres is available (not only reachable over TCP)

if [[ $1 =~ ^(.+):(.*)@(.+):(.+)/(.+)$ ]]; 
then 
  USER=${BASH_REMATCH[1]};
  PASSWORD=${BASH_REMATCH[2]};
  HOST=${BASH_REMATCH[3]};
  PORT=${BASH_REMATCH[4]};
  DATABASE=${BASH_REMATCH[5]};
else 
    echo "Usage: $0 user:password@host:port/database [-- command args]"
    exit 1
fi
shift
if [[ $1 == "--" ]]; then
    shift
    WAITFORIT_CLI=("$@")
fi

test() {
    node - $HOST $USER $PASSWORD $DATABASE $PORT << EOF
    const Client = require('pg').Client;
    const client = new Client({host: process.argv[2], user: process.argv[3], password: process.argv[4], database: process.argv[5], port: Number(process.argv[6])})

    const main = async () => {
        try {
            await client.connect()
            const res = await client.query('select 1') 
        } catch(e) {
            process.exit(1)
        }
    }
    main().then(() => process.exit(0), () => process.exit(1))
EOF
}

while ! test; do
  sleep 0.1 # wait for 1/10 of the second before check again
done

echo "Postgres SELECT 1 was successful on $HOST"
if [[ $WAITFORIT_CLI != "" ]]; then
    exec ${WAITFORIT_CLI[@]}
fi
