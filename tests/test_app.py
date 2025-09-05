import unittest
import os
from app import app, db, User
from mongomock import MongoClient

import app as main_app

class SycordTestCase(unittest.TestCase):
    def setUp(self):
        """Set up a test client and a mock database."""
        main_app.app.config['TESTING'] = True
        main_app.app.config['WTF_CSRF_ENABLED'] = False
        main_app.app.config['SECRET_KEY'] = 'test-secret'

        # Use mongomock for a mock MongoDB database and patch the app's db object
        self.mongo_client = MongoClient()
        main_app.db = self.mongo_client.sycord_test

        self.app = main_app.app.test_client()

        # Create a user and log them in for tests that require authentication
        with self.app as client:
            with client.session_transaction() as sess:
                user_data = {"id": "12345", "username": "testuser", "email": "test@example.com"}
                sess["_user_id"] = "12345"
                sess["user"] = user_data
                # We also need to create the user in the mock DB
                main_app.db.users.insert_one({"_id": "12345", "username": "testuser", "email": "test@example.com", "bots": []})


    def tearDown(self):
        """Clean up the mock database."""
        self.mongo_client.close()

    def test_index_page(self):
        """Test that the index page loads."""
        response = self.app.get('/', follow_redirects=True)
        self.assertEqual(response.status_code, 200)

    def test_dashboard_page_authenticated(self):
        """Test that the dashboard is accessible to an authenticated user."""
        response = self.app.get('/dashboard', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Your Bots', response.data)

    def test_create_bot(self):
        """Test creating a new bot."""
        response = self.app.post('/create_bot', data={
            'bot_name': 'TestBot',
            'startup_command': 'python main.py'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        # Check if the bot was added to the database
        bot = main_app.db.bots.find_one({'name': 'TestBot'})
        self.assertIsNotNone(bot)
        self.assertEqual(bot['owner_id'], '12345')

        # Check if the bot was added to the user's bot list
        user = main_app.db.users.find_one({'_id': '12345'})
        self.assertIn(bot['_id'], user['bots'])

    def test_file_operations(self):
        """Test file creation, reading, and deletion with the new data structure."""
        # First, create a bot to work with
        self.app.post('/create_bot', data={'bot_name': 'FileBot', 'startup_command': 'run'}, follow_redirects=True)
        bot = main_app.db.bots.find_one({'name': 'FileBot'})
        bot_id = str(bot['_id'])

        # Create a file
        create_response = self.app.post(f'/api/bot/{bot_id}/files/create', json={'path': 'main.py', 'type': 'file'})
        self.assertEqual(create_response.status_code, 200)

        # Check the database directly
        bot_after_create = main_app.db.bots.find_one({'_id': bot['_id']})
        self.assertEqual(len(bot_after_create['files']), 1)
        self.assertEqual(bot_after_create['files'][0]['path'], 'main.py')

        # Verify file content
        get_response = self.app.get(f'/api/bot/{bot_id}/file?path=main.py')
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json['content'], '')

        # Update file content
        update_response = self.app.post(f'/api/bot/{bot_id}/file?path=main.py', json={'content': 'print("hello")'})
        self.assertEqual(update_response.status_code, 200)

        bot_after_update = main_app.db.bots.find_one({'_id': bot['_id']})
        self.assertEqual(bot_after_update['files'][0]['content'], 'print("hello")')

        # Delete the file
        delete_response = self.app.post(f'/api/bot/{bot_id}/files/delete', json={'path': 'main.py'})
        self.assertEqual(delete_response.status_code, 200)

        bot_after_delete = main_app.db.bots.find_one({'_id': bot['_id']})
        self.assertEqual(len(bot_after_delete['files']), 0)


if __name__ == '__main__':
    unittest.main()
