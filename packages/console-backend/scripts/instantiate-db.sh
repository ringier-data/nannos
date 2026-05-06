# instantiate postgres database for agent-console backend

set -e

DB_NAME="console"
DB_USER="postgres"
DB_PASSWORD="password"
DB_HOST="localhost"
DB_PORT="5432"
export PGPASSWORD=$DB_PASSWORD

# set all users in the users table to be is_administrator = true
psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d postgres <<EOF
UPDATE users SET is_administrator = true;
EOF

# create 'nannos-team' user group if it doesn't exist
psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME <<EOF
DO
\$do\$
BEGIN
   IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nannos-team') THEN
      CREATE ROLE "nannos-team";
   END IF;
END
\$do\$;
EOF
