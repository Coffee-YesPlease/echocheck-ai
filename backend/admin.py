"""Local administrator provisioning and audio retention commands."""
from __future__ import annotations
import argparse
import getpass
import sqlite3
from .config import Settings
from .store import Store

def main():
    parser = argparse.ArgumentParser(description='EchoCheck local administrator tools')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-user', help='Provision a named participant; password is entered privately')
    create.add_argument('--username', required=True)
    create.add_argument('--role', choices=['supervisor','worker'], required=True)
    create.add_argument('--site', required=True)
    create.add_argument('--display-name', required=True)
    prototype = commands.add_parser('bootstrap-prototype', help='Create the three local prototype accounts')
    prototype.add_argument('--reset', action='store_true', help='Explicitly reset the three prototype passwords and sessions')
    commands.add_parser('purge-expired', help='Remove expired recording blobs and retain their audit records')
    args = parser.parse_args()
    store = Store(Settings.from_env())
    if args.command == 'bootstrap-prototype':
        from .prototype import bootstrap_prototype
        try:
            profiles = bootstrap_prototype(store,reset=args.reset)
        except ValueError as exc:
            parser.error(str(exc))
        print(f'Prepared {len(profiles)} local prototype profiles.')
    elif args.command == 'create-user':
        password = getpass.getpass('Password (minimum 12 characters): ')
        if password != getpass.getpass('Confirm password: '):
            parser.error('Passwords do not match')
        try:
            user = store.create_user(args.username,password,args.role,args.site,args.display_name)
        except (ValueError,sqlite3.IntegrityError) as exc:
            parser.error(str(exc) if isinstance(exc,ValueError) else 'Username already exists')
        print(f"Created {user['role']} {user['username']} for site {user['site']}.")
    else:
        print(f'Removed {store.purge_expired()} expired recording files.')

if __name__ == '__main__':
    main()
