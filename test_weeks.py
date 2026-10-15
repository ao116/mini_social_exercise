import sqlite3
from datetime import datetime, timedelta

# Change this to the ID of the user you are testing with
USER_ID = 539

def insert_weekly_test_posts(scenario):
    conn = sqlite3.connect('database.sqlite')
    cursor = conn.cursor()
    
    # Base time: Let's pretend the first post was made exactly 28 days (4 weeks) ago
    start_time = datetime.utcnow() - timedelta(days=28)
    posts = []

    if scenario == 'perfect_week':
        print("Inserting a perfect weekly streak...")
        posts = [
            start_time,                                      # Week 1
            start_time + timedelta(days=7, hours=2),         # Week 2
            start_time + timedelta(days=14, hours=2),        # Week 3
            start_time + timedelta(days=21, hours=2)         # Week 4
        ]
    elif scenario == 'broken_week':
        print("Inserting a broken weekly streak (missed week 3)...")
        posts = [
            start_time,                                      # Week 1
            start_time + timedelta(days=7, hours=2),         # Week 2
            # Missed the week 3 window!
            # start_time + timedelta(days=21, hours=2)         # Week 4 - This will trigger a reset!
        ]
    
    # Insert the posts into the database
    for i, p_time in enumerate(posts):
        formatted_time = p_time.strftime('%Y-%m-%d %H:%M:%S')
        content = f"Weekly test post {i+1} for scenario: {scenario}"
        cursor.execute(
            'INSERT INTO posts (user_id, content, created_at) VALUES (?, ?, ?)',
            (USER_ID, content, formatted_time)
        )
        print(f"Inserted post at: {formatted_time}")

    conn.commit()
    conn.close()
    print("Done! Go check your /tasks page.\n")

if __name__ == '__main__':
    conn = sqlite3.connect('database.sqlite')
    
    # 1. Wipe old test posts for this user to start fresh
    conn.execute('DELETE FROM posts WHERE user_id = ? AND content LIKE "%test post%"', (USER_ID,))
    
    # 2. Reset the user's task progress. 
    # We set weekly_unlocked to 1 so we can bypass the daily challenge for this test.
    conn.execute('UPDATE user_tasks SET weekly_unlocked = 1, monthly_unlocked = 0 WHERE user_id = ?', (USER_ID,))
    
    conn.commit()
    conn.close()

    # CHOOSE YOUR SCENARIO HERE:
    # Change 'perfect_week' to 'broken_week' to test the reset logic
    insert_weekly_test_posts('perfect_week')