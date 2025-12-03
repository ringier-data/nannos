#!/bin/bash
# =============================================================================
#
# This file *MUST* be saved with executable permissions. Otherwise, Ansible will try to parse as a password file and display:
# "ERROR! Decryption failed"
#
# For manual usage:
#    ansible-vault --vault-id ./ops-ci-shared/misc/ansible-vault-password.sh view some_encrypted_file
#
# For automation usage, just create `./infrastructure/ansible.cfg`, with the following content:
#    ```
#    [defaults]
#
#    vault_password_file = ../ops-ci-shared/misc/ansible-vault-password.sh
#    ```
#
# Both will retrieve the password from the AWS SSM parameter named '/ops-ci/ansible-vault-password'. Your user/role must have the
# permission to decrypt using the alias/ssm key.
#
# =============================================================================

SSM_PARAM_KEY="/ops-ci/ansible-vault-password"

aws ssm get-parameter --name "$SSM_PARAM_KEY" --output text --query 'Parameter.Value' --with-decrypt
