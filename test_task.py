import sqlite3
from datetime import datetime, timedelta

# Change this to the ID of the user you are testing with!
# (Usually 1 if it's the first account you made)
USER_ID = 539

def insert_test_posts(scenario):
    conn = sqlite3.connect('database.sqlite')
    cursor = conn.cursor()
    
    # Base time: Let's pretend the first post was made exactly 7 days ago
    start_time = datetime.utcnow() - timedelta(days=7)
    posts = []

    if scenario == 'perfect':
        print("Inserting a perfect streak...")
        posts = [
            start_time,                                     # Window 0 (Days 1-2)
            start_time + timedelta(days=2, hours=2),        # Window 1 (Days 3-4)
            start_time + timedelta(days=4, hours=2),        # Window 2 (Days 5-6)
            start_time + timedelta(days=6, hours=2)         # Window 3 (Days 7-8)
        ]
    elif scenario == 'broken_streak':
        print("Inserting a broken streak (missed window 2)...")
        posts = [
            start_time,                                     # Window 0 (Days 1-2)
            start_time + timedelta(days=2, hours=2),        # Window 1 (Days 3-4)
            # Missed the 4-6 day window!
            # start_time + timedelta(days=6, hours=2)         # Window 3 (Days 7-8) - This will trigger a reset!
        ]
    
    # Insert the posts into the database
    for i, p_time in enumerate(posts):
        formatted_time = p_time.strftime('%Y-%m-%d %H:%M:%S')
        content = f"Test post {i+1} for scenario: {scenario}"
        cursor.execute(
            'INSERT INTO posts (user_id, content, created_at) VALUES (?, ?, ?)',
            (USER_ID, content, formatted_time)
        )
        print(f"Inserted post at: {formatted_time}")

    conn.commit()
    conn.close()
    print("Done! Go check your /tasks page.\n")

if __name__ == '__main__':
    # Wipe old test posts for this user to start fresh
    # conn = sqlite3.connect('database.sqlite')
    # conn.execute('DELETE FROM posts WHERE user_id = ? AND content LIKE "Test post%"', (USER_ID,))
    # conn.commit()
    # conn.close()
    conn = sqlite3.connect('database.sqlite')
    
    # 1. Wipe old test posts for this user
    conn.execute('DELETE FROM posts WHERE user_id = ?', (USER_ID,))
    
    # 2. Reset the user's task unlock progress back to zero
    conn.execute('''UPDATE user_tasks SET weekly_unlocked = 0, monthly_unlocked = 0, monthly_completed = 0,
                    daily_completed_at = NULL, weekly_completed_at = NULL
                    WHERE user_id = ?''', (USER_ID,))
    
    conn.commit()
    conn.close()

    # CHOOSE YOUR SCENARIO HERE:
    insert_test_posts('perfect')

    # CHOOSE YOUR SCENARIO HERE:
    # Change 'perfect' to 'broken_streak' to test the reset logic
    # insert_test_posts('perfect')