import asyncio
import sqlite3
import sys
import re
import logging
import os
from datetime import datetime
from telethon import TelegramClient, events, functions, types
from telethon.errors import *
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest, GetParticipantsRequest
from telethon.tl.types import InputPeerEmpty, ChannelParticipantsSearch

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_operations.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('telethon.network').setLevel(logging.WARNING)


from telethon.sessions import SQLiteSession, StringSession    # ← NEW import

# ------------------------------------------------------------------
# Helpers to open the same .session file safely
# ------------------------------------------------------------------
def make_client(session_path: str, api_id: int, api_hash: str):
    """
    Return a TelegramClient that opens *one* SQLite connection with a long
    busy-timeout, so the file will patiently wait instead of throwing
    “database is locked”.
    """
    return TelegramClient(SQLiteSession(session_path), api_id, api_hash)


def load_string_session(session_path: str):
    """
    Convert an on-disk session into an in-memory StringSession so we can
    *read* it (Check login, show the name, …) without ever touching the file.
    """
    if not os.path.exists(session_path + '.session'):
        return None

    # one short-lived connection; then everything stays in RAM
    temp = SQLiteSession(session_path)
    return StringSession(StringSession.save(temp))



class TelegramAccountManager:
    def __init__(self):
        self.accounts_db = 'accounts.db'
        self._init_db()

    def _init_db(self):
        # Ensure the directory exists
        if os.path.dirname(self.accounts_db):
            os.makedirs(os.path.dirname(self.accounts_db))
        else:
            None

        # Use a single connection per instance
        self.conn = sqlite3.connect(
            self.accounts_db,
            timeout=30,
            check_same_thread=False,
            isolation_level=None  # Disable transactions for better concurrency
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
        self.conn.execute('''CREATE TABLE IF NOT EXISTS accounts
                           (phone TEXT PRIMARY KEY, 
                            api_id INTEGER, 
                            api_hash TEXT, 
                            session_path TEXT)''')
        self.conn.commit()

    def __del__(self):
        if hasattr(self, 'conn'):
            self.conn.close()

    def add_account(self, phone, api_id, api_hash):
        session_path = os.path.join('sessions', phone)
        os.makedirs(session_path, exist_ok=True)
        try:
            self.conn.execute('''INSERT OR REPLACE INTO accounts 
                               (phone, api_id, api_hash, session_path) 
                               VALUES (?, ?, ?, ?)''',
                              (phone, api_id, api_hash, session_path))
            self.conn.commit()
            return os.path.join(session_path, phone)
        except sqlite3.Error as e:
            logger.error(f"Database error adding account: {e}")
            return None

    def delete_account(self, phone):
        try:
            self.conn.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error deleting account: {e}")
            return False

    def list_accounts(self):
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT phone, api_id, api_hash FROM accounts")
            return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"Database error listing accounts: {e}")
            return []


class TelegramBotManager:
    def __init__(self, client=None):
        self.client = client
        if client:
            os.makedirs('scraped_users', exist_ok=True)
            os.makedirs('joinable_groups', exist_ok=True)
            self.dm_db = os.path.join('sessions', client.session.filename.split('.')[0], 'dm_tracker.db')
            self._init_dm_db()

    def _init_dm_db(self):
        # NEW — make parent directory if it’s missing
        os.makedirs(os.path.dirname(self.dm_db), exist_ok=True)

        with sqlite3.connect(self.dm_db, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS dmed_users
                              (user_id TEXT PRIMARY KEY,
                               timestamp DATETIME)''')
            conn.commit()

    async def get_account_info(self):
        me = await self.client.get_me()
        return {
            'name': f"{me.first_name} {me.last_name or ''}",
            'username': f"@{me.username}" if me.username else "None",
            'phone': f"+{me.phone}",
            'id': me.id
        }

    async def get_entity_members(self, entity_input, limit=None):
        try:
            entity = await self.client.get_entity(entity_input)
            members = []
            offset = 0
            total_count = 0
            last_count = 0

            while True:
                participants = await self.client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsSearch(''),
                    offset=offset,
                    limit=200,
                    hash=0
                ))

                if not participants.users:
                    break

                new_members = participants.users
                members.extend(new_members)
                offset += len(new_members)
                total_count += len(new_members)

                if total_count - last_count >= 1000:
                    logger.info(f"Fetched {total_count} members...")
                    last_count = total_count

                if limit and total_count >= limit:
                    break

                await asyncio.sleep(1)

            logger.info(f"Total members fetched: {total_count}")

            # Sanitize the filename
            if hasattr(entity, 'username') and entity.username:
                clean_name = entity.username
            else:
                # Remove URL parts and special characters
                clean_name = re.sub(r'https?://(t\.me/)?', '', str(entity_input))
                clean_name = re.sub(r'[^\w\-_]', '_', clean_name)

            filename = f"members_{clean_name}_{datetime.now().strftime('%Y%m%d')}.txt"
            os.makedirs('scraped_users', exist_ok=True)
            filepath = os.path.join('scraped_users', filename)
            print(filepath)

            with open(filepath, 'w', encoding='utf-8') as f:

                f.write('\n'.join([
                    f"{m.id},{m.username or ''},{m.first_name or ''},{m.last_name or ''}"
                    for m in members
                ]))

            logger.info(f"Saved {len(members)} members to {filepath}")
            return members

        except Exception as e:
            logger.error(f"Error getting members: {e}")
            return []

    async def add_users_to_group(self, group_input, user_file):
        try:
            try:
                await self.client(JoinChannelRequest(group_input))
                logger.info(f"Joined target group {group_input}")
            except Exception as e:
                logger.warning(f"Could not join group {group_input}: {e}")

            try:
                group = await self.client.get_entity(group_input)
            except Exception as e:
                logger.error(f"Could not get entity for group {group_input}: {e}")
                return 0

            added_count = 0
            current_members = await self.get_entity_members(group_input)
            current_member_ids = {str(m.id) for m in current_members}

            with open(user_file, 'r', encoding='utf-8') as f:
                users = [line.strip().split(',')[0] for line in f if line.strip()]

            for user_id in users:
                try:
                    if str(user_id) in current_member_ids:
                        continue

                    if not self._is_user_dmed(user_id):
                        try:
                            user = await self.client.get_entity(int(user_id))
                        except ValueError:
                            user = await self.client.get_entity(user_id)

                        await self.client(InviteToChannelRequest(group, [user]))
                        logger.info(f"Added {user_id} to {group_input}")
                        added_count += 1
                        self._mark_user_dmed(user_id)
                        await asyncio.sleep(10)
                except FloodWaitError as e:
                    wait_time = e.seconds
                    logger.warning(f"Flood wait for {wait_time} seconds")
                    await asyncio.sleep(wait_time)
                except UserPrivacyRestrictedError:
                    logger.warning(f"User {user_id} has privacy restrictions")
                except Exception as e:
                    logger.error(f"Failed to add {user_id}: {e}")

            return added_count
        except Exception as e:
            logger.error(f"Error in add_users_to_group: {e}")
            return 0

    async def join_groups(self, group_file):
        success_count = 0
        with open(group_file, 'r', encoding='utf-8') as f:
            groups = [line.strip() for line in f if line.strip()]

        for group in groups:
            try:
                await self.client(JoinChannelRequest(group))
                logger.info(f"Joined {group}")
                success_count += 1
                await asyncio.sleep(20)
            except Exception as e:
                logger.error(f"Could not join {group}: {e}")
        return success_count

    async def send_group_messages(self, interval):
        message_file = os.path.join('sessions', self.client.session.filename.split('.')[0], 'message.txt')
        if not os.path.exists(message_file):
            logger.error(f"Message file not found: {message_file}")
            return

        with open(message_file, 'r', encoding='utf-8') as f:
            message = f.read()

        while True:
            result = self.client.iter_dialogs(offset_date=None, offset_id=0, offset_peer=InputPeerEmpty(), limit=200)
            async for chat in result:
                try:
                    if chat.is_group:
                        group_entity = await self.client.get_entity(chat.id)
                        group_name = f"{group_entity.title} [id:{group_entity.id}]"
                        if hasattr(group_entity, 'username'):
                            group_name += f" @{group_entity.username}"
                        logger.info(f"Sending message to group: {group_name}")
                        await self.client.send_message(int(chat.id), message, parse_mode="HTML")
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Failed to send to {chat.name}: {e}")

            logger.info(f'Sleep {interval} seconds')
            await asyncio.sleep(interval)

    async def send_direct_messages_single_account(self, user_file, message):
        sent_count = 0
        with open(user_file, 'r', encoding='utf-8') as f:
            users = [line.strip().split(',')[0] for line in f if line.strip()]

        for user_id in users:
            try:
                if not self._is_user_dmed(user_id):
                    try:
                        entity = await self.client.get_entity(int(user_id))
                    except ValueError:
                        entity = await self.client.get_entity(user_id)

                    await self.client.send_message(entity, message)
                    self._mark_user_dmed(user_id)
                    sent_count += 1
                    await asyncio.sleep(5)
            except FloodWaitError as e:
                wait_time = e.seconds
                logger.warning(f"Flood wait for {wait_time} seconds")
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Could not message {user_id}: {e}")
        return sent_count

    async def start_auto_reply(self, reply_text):
        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            if event.is_private:
                await asyncio.sleep(10)
                try:
                    await event.reply(reply_text)
                except Exception as e:
                    logger.error(f"Failed to send auto-reply: {e}")

        await self.client.run_until_disconnected()

    async def search_entities(self, query):
        try:
            result = await self.client(functions.contacts.SearchRequest(
                q=query,
                limit=200
            ))

            entities = {
                'channels': [],
                'groups': [],
                'users': []
            }

            for chat in result.chats:
                if chat.broadcast:
                    entities['channels'].append(chat)
                elif chat.megagroup:
                    entities['groups'].append(chat)

            for user in result.users:
                entities['users'].append(user)

            return entities
        except Exception as e:
            logger.error(f"Search error: {e}")
            return None

    def _is_user_dmed(self, user_id):
        with sqlite3.connect(self.dm_db, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM dmed_users WHERE user_id = ?", (user_id,))
            return cursor.fetchone() is not None

    def _mark_user_dmed(self, user_id):
        with sqlite3.connect(self.dm_db, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT OR REPLACE INTO dmed_users
                             (user_id, timestamp) VALUES (?, datetime('now'))''',
                           (user_id,))
            conn.commit()

    def list_scraped_user_files(self):
        return [f for f in os.listdir('scraped_users')
                if f.endswith('.txt') and os.path.isfile(os.path.join('scraped_users', f))]

    def list_joinable_group_files(self):
        return [f for f in os.listdir('joinable_groups')
                if f.endswith('.txt') and os.path.isfile(os.path.join('joinable_groups', f))]


async def send_direct_messages_multi_account(account_manager, selected_accounts, user_file, message):
    total_sent = 0
    user_ids = []

    with open(user_file, 'r', encoding='utf-8') as f:
        user_ids = [line.strip().split(',')[0] for line in f if line.strip()]

    for account_idx, (phone, api_id, api_hash) in enumerate(selected_accounts):
        try:
            #client = TelegramClient(os.path.join('sessions', phone, phone), api_id, api_hash)
            session_path = os.path.join('sessions', phone, phone)
            client = make_client(session_path, api_id, api_hash)  # ← uses long timeout

            await client.start()

            me = await client.get_me()
            manager = TelegramBotManager(client)

            users_per_account = len(user_ids) // len(selected_accounts)
            start_idx = account_idx * users_per_account
            end_idx = start_idx + users_per_account if account_idx < len(selected_accounts) - 1 else len(user_ids)
            account_user_ids = user_ids[start_idx:end_idx]

            for user_id in account_user_ids:
                try:
                    if not manager._is_user_dmed(user_id):
                        try:
                            entity = await client.get_entity(int(user_id))
                        except ValueError:
                            entity = await client.get_entity(user_id)

                        await client.send_message(entity, message)
                        manager._mark_user_dmed(user_id)
                        total_sent += 1
                        await asyncio.sleep(5)
                except FloodWaitError as e:
                    wait_time = e.seconds
                    await asyncio.sleep(wait_time)
                except Exception as e:
                    logger.error(f"Could not message {user_id} from {phone}: {e}")

            await client.disconnect()
        except Exception as e:
            logger.error(f"Error with account {phone}: {e}")
            continue

    return total_sent


async def get_login_code(phone, api_id, api_hash):
    session_path = os.path.join('sessions', phone, phone)
    client = TelegramClient(session_path, api_id, api_hash)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)

            @client.on(events.NewMessage(incoming=True))
            async def handler(event):
                if "login code" in event.raw_text.lower():
                    print(f"\nLogin code found in message: {event.raw_text}")
                    await client.disconnect()

            print("\nWaiting for login code in Telegram messages...")
            await client.run_until_disconnected()
    except Exception as e:
        print(f"\nError during login: {e}")
    finally:
        await client.disconnect()


async def login_new_account(phone, api_id, api_hash):
    #session_path = os.path.join('sessions', phone, phone)
    #client = TelegramClient(session_path, api_id, api_hash)
    session_path = os.path.join('sessions', phone, phone)
    client = make_client(session_path, api_id, api_hash)  # ← uses long timeout

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input("Enter the code you received: ")
            await client.sign_in(phone, code)

            if isinstance(await client.get_me(), types.UserEmpty):
                password = input("Enter your 2FA password: ")
                await client.sign_in(password=password)

            me = await client.get_me()
            print(f"\nSuccessfully logged in as: {me.first_name} ({phone})")
        else:
            me = await client.get_me()
            print(f"\nAlready logged in as: {me.first_name} ({phone})")

        return True
    except Exception as e:
        print(f"\nError during login: {e}")
        return False
    finally:
        await client.disconnect()


def show_main_menu():
    print('\nTelegram Bot Manager')
    print('=' * 40)
    print('[1] Account Manager')
    print('[2] Retrieve Group/Channel Members')
    print('[3] Add Members to Group')
    print('[4] Join Groups/Channels')
    print('[5] Send Message to Groups')
    print('[6] Send Direct Messages')
    print('[7] Auto Reply Bot')
    print('[8] Search for Entities')
    print('[0] Exit')
    print('=' * 40)

    try:
        return int(input('Enter your choice: '))
    except ValueError:
        return -1


def select_file(file_list, directory):
    if not file_list:
        print("No files found in directory!")
        return None

    print("\nAvailable files:")
    for i, filename in enumerate(file_list, 1):
        print(f"{i}. {filename}")

    try:
        choice = int(input("\nSelect file by number: ")) - 1
        if 0 <= choice < len(file_list):
            return os.path.join(directory, file_list[choice])
    except ValueError:
        pass

    print("Invalid selection!")
    return None


async def select_account(account_manager, allow_multiple=False):
    accounts = account_manager.list_accounts()
    print(100 * '!')
    print(accounts)
    if not accounts:
        return None

    print("\nAvailable accounts:")
    print('no')

    for i, (phone, api_id, api_hash) in enumerate(accounts, 1):
        try:
            session_path = os.path.join('sessions', phone, phone)
            string_session = load_string_session(session_path)
            if string_session:
                async with TelegramClient(string_session, api_id, api_hash) as temp_client:



                    if await temp_client.is_user_authorized():

                        me = await temp_client.get_me()
                        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
                        username = f"@{me.username}" if me.username else "No username"
                        print(f"{i}. {phone} - {name} ({username}) - API: {api_id}/{api_hash}")
                    else:
                        print(f"{i}. {phone} - Not logged in - API: {api_id}/{api_hash}")
            else:
                print(f"{i}. {phone} - No session - API: {api_id}/{api_hash}")
        except Exception as e:
            print(f"{i}. {phone} - Error checking status: {str(e)} - API: {api_id}/{api_hash}")

    if allow_multiple:
        print("\nEnter account numbers separated by commas (e.g., 1,3,5)")
        try:
            choices = input("Select accounts: ").strip().split(',')
            selected = []
            for choice in choices:
                idx = int(choice.strip()) - 1
                if 0 <= idx < len(accounts):
                    selected.append(accounts[idx])
            return selected if selected else None
        except ValueError:
            pass
    else:
        try:
            choice = int(input("\nSelect account by number: ")) - 1
            if 0 <= choice < len(accounts):
                return [accounts[choice]]
        except ValueError:
            pass

    print("Invalid selection!")
    return None


async def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    account_manager = TelegramAccountManager()
    accounts = account_manager.list_accounts()
    print(accounts)
    if not accounts:
        print("No accounts configured. Please add your first account.")
        phone = input("Enter phone number (with country code): ")
        api_id = int(input("Enter API ID: "))
        api_hash = input("Enter API Hash: ")
        account_manager.add_account(phone, api_id, api_hash)
        if await login_new_account(phone, api_id, api_hash):
            accounts = account_manager.list_accounts()
        else:
            print("Failed to login to new account")
            return

    selected_account = await select_account(account_manager)
    if not selected_account:
        return

    phone, api_id, api_hash = selected_account[0]

    try:
        #client = TelegramClient(os.path.join('sessions', phone, phone), api_id, api_hash)
        session_path = os.path.join('sessions', phone, phone)
        client = make_client(session_path, api_id, api_hash)  # ← uses long timeout

        await client.start()
        me = await client.get_me()
        print(f"\nLogged in as: {me.first_name} ({phone})")
        manager = TelegramBotManager(client)
        while True:
            choice = show_main_menu()
            if choice == 1:
                while True:
                    print('\nAccount Manager')
                    print('=' * 30)
                    print('[1] List Accounts')
                    print('[2] Add Account')
                    print('[3] Delete Account')
                    print('[4] Get Login Code')
                    print('[0] Back')
                    print('=' * 30)

                    sub_choice = input('Enter choice: ')

                    if sub_choice == '1':
                        accounts = account_manager.list_accounts()
                        print("\nConfigured Accounts:")
                        for i, (phone, api_id, api_hash) in enumerate(accounts, 1):
                            try:
                                session_path = os.path.join('sessions', phone, phone)
                                if os.path.exists(session_path + '.session'):
                                    async with TelegramClient(session_path, api_id, api_hash) as temp_client:
                                        # await temp_client.connect()
                                        if await temp_client.is_user_authorized():
                                            me = await temp_client.get_me()
                                            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
                                            username = f"@{me.username}" if me.username else "No username"
                                            print(f"{i}. {phone} - {name} ({username}) - API: {api_id}/{api_hash}")
                                        else:
                                            print(f"{i}. {phone} - Not logged in - API: {api_id}/{api_hash}")
                                else:
                                    print(f"{i}. {phone} - No session - API: {api_id}/{api_hash}")
                            except:
                                print(f"{i}. {phone} - Error checking status - API: {api_id}/{api_hash}")

                    elif sub_choice == '2':
                        phone = input("Phone number (with country code): ")
                        api_id = int(input("API ID: "))
                        api_hash = input("API Hash: ")
                        account_manager.add_account(phone, api_id, api_hash)
                        print("Account added successfully!")
                        await login_new_account(phone, api_id, api_hash)

                    elif sub_choice == '3':
                        accounts = account_manager.list_accounts()
                        selected = await select_account(account_manager)
                        if selected:
                            phone = selected[0][0]
                            account_manager.delete_account(phone)
                            print(f"Account {phone} deleted successfully!")

                    elif sub_choice == '4':
                        accounts = account_manager.list_accounts()
                        selected = await select_account(account_manager)
                        if selected:
                            phone, api_id, api_hash = selected[0]
                            await get_login_code(phone, api_id, api_hash)

                    elif sub_choice == '0':
                        break

                    else:
                        print("Invalid choice!")

            elif choice == 2:
                entity = input("Enter group/channel username or link: ")
                limit_input = input("Max members to fetch (enter 'max' for all or a number): ")

                limit = None if limit_input.lower() == 'max' else int(limit_input)

                print("\nFetching members...")
                members = await manager.get_entity_members(entity, limit)

            elif choice == 3:
                group = input("Target group/channel username: ")
                user_files = manager.list_scraped_user_files()
                user_file = select_file(user_files, 'scraped_users')

                if user_file:
                    all_accounts = account_manager.list_accounts()
                    print("\nSelect accounts to use for adding members:")
                    selected_accounts = await select_account(account_manager, allow_multiple=True)

                    if selected_accounts:
                        total_added = 0
                        for account in selected_accounts:
                            phone, api_id, api_hash = account
                            try:
                                client = TelegramClient(os.path.join('sessions', phone, phone), api_id, api_hash)
                                await client.start()
                                manager = TelegramBotManager(client)
                                added = await manager.add_users_to_group(group, user_file)
                                await client.disconnect()
                                total_added += added
                                print(f"\nAccount {phone} added {added} users")
                            except Exception as e:
                                print(f"\nError with account {phone}: {e}")
                        print(f"\nTotal users added: {total_added}")

            elif choice == 4:
                group_files = manager.list_joinable_group_files()
                group_file = select_file(group_files, 'joinable_groups')

                if group_file:
                    joined = await manager.join_groups(group_file)
                    print(f"\nSuccessfully joined {joined} groups")

            elif choice == 5:
                interval = int(input("Interval between broadcasts (seconds): "))
                await manager.send_group_messages(interval)

            elif choice == 6:
                user_files = manager.list_scraped_user_files()
                user_file = select_file(user_files, 'scraped_users')

                if user_file:
                    message = input("Message to send: ")
                    all_accounts = account_manager.list_accounts()
                    print("\nSelect accounts for DM campaign:")
                    selected_accounts = await select_account(account_manager, allow_multiple=True)

                    if selected_accounts:
                        if len(selected_accounts) == 1:
                            phone, api_id, api_hash = selected_accounts[0]
                            client = TelegramClient(os.path.join('sessions', phone, phone), api_id, api_hash)
                            await client.start()
                            manager = TelegramBotManager(client)
                            sent = await manager.send_direct_messages_single_account(user_file, message)
                            await client.disconnect()
                        else:
                            sent = await send_direct_messages_multi_account(account_manager, selected_accounts,
                                                                            user_file, message)
                        print(f"\nSuccessfully sent to {sent} users")

            elif choice == 7:
                reply_msg = input("Enter auto-reply message: ")
                print("\nAuto-reply bot started with 10 second delay")
                print("Press Ctrl+C to stop")
                try:
                    await manager.start_auto_reply(reply_msg)
                except KeyboardInterrupt:
                    print("\nAuto-reply stopped")

            elif choice == 8:
                query = input("Enter search query: ")
                results = await manager.search_entities(query)

                if results:
                    print(f"\nSearch results for '{query}':")
                    print(f"Channels: {len(results['channels'])}")
                    print(f"Groups: {len(results['groups'])}")
                    print(f"Users: {len(results['users'])}")

                    if input("\nShow details? (y/n): ").lower() == 'y':
                        print("\nTop 5 results in each category:")

                        print("\nChannels:")
                        for i, channel in enumerate(results['channels'][:5], 1):
                            name = f"{channel.title} [id:{channel.id}]"
                            if hasattr(channel, 'username'):
                                name += f" @{channel.username}"
                            print(f"{i}. {name}")

                        print("\nGroups:")
                        for i, group in enumerate(results['groups'][:5], 1):
                            name = f"{group.title} [id:{group.id}]"
                            if hasattr(group, 'username'):
                                name += f" @{group.username}"
                            print(f"{i}. {name}")

                        print("\nUsers:")
                        for i, user in enumerate(results['users'][:5], 1):
                            name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                            print(f"{i}. {name} (@{user.username or 'N/A'})")

            elif choice == 0:
                await client.disconnect()
                print("\nGoodbye!")
                sys.exit(0)

            input("\nPress Enter to continue...")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    os.makedirs('sessions', exist_ok=True)
    os.makedirs('scraped_users', exist_ok=True)
    os.makedirs('joinable_groups', exist_ok=True)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScript interrupted ")
        sys.exit(0)