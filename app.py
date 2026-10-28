from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
import math
import random
import time
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    # 1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    # 2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions,
                   IFNULL(t.weekly_unlocked, 0) as daily_done,
                   IFNULL(t.monthly_unlocked, 0) as weekly_done,
                   IFNULL(t.monthly_completed, 0) as monthly_done
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN user_tasks t ON u.id = t.user_id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(t.weekly_unlocked, 0) as daily_done,
                   IFNULL(t.monthly_unlocked, 0) as weekly_done,
                   IFNULL(t.monthly_completed, 0) as monthly_done
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN user_tasks t ON u.id = t.user_id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        
        # Comments query with badge joins included
        comments_raw = query_db("""
            SELECT c.id, c.content, c.created_at, u.username, u.id as user_id,
                   IFNULL(t.weekly_unlocked, 0) as daily_done,
                   IFNULL(t.monthly_unlocked, 0) as weekly_done,
                   IFNULL(t.monthly_completed, 0) as monthly_done
            FROM comments c 
            JOIN users u ON c.user_id = u.id 
            LEFT JOIN user_tasks t ON u.id = t.user_id 
            WHERE c.post_id = ? 
            ORDER BY c.created_at ASC
        """, (post['id'],))
        
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
            
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    # 4. Render Template with Pagination Info 
    return render_template('feed.html.j2',
                           posts=posts_data,
                           pulse=community_pulse(),
                           welcome_queue=welcome_queue(current_user_id),
                           newcomer=newcomer_status(current_user_id),
                           my_streak=clean_streak(current_user_id) if current_user_id else None,
                           current_sort=sort,
                           current_show=show,
                           page=page,
                           per_page=POSTS_PER_PAGE,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

                           
@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Pass the user's content through the moderation function
    moderated_content = content

    # Newcomers can post once a day in their first week (Assignment 3.2, DC25)
    wait = newcomer_post_wait(user_id)
    if wait:
        hours = max(1, round(wait.total_seconds() / 3600))
        flash(f'New members can share one post per day during their first week. '
              f'You can post again in about {hours} hour(s) - comments are always open!', 'info')
        return redirect(url_for('feed'))

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        cur = db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                         (user_id, moderated_content))
        db.commit()
        update_user_tasks(user_id)  # unlock challenges/badges right away
        assign_welcomers(cur.lastrowid, user_id)  # Assignment 3.2, DC20
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           streak=clean_streak(user['id']))
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               IFNULL(t.weekly_unlocked, 0) as daily_done,
               IFNULL(t.monthly_unlocked, 0) as weekly_done,
               IFNULL(t.monthly_completed, 0) as monthly_done
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN user_tasks t ON u.id = t.user_id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db("""
        SELECT c.id, c.content, c.created_at, u.username, u.id as user_id,
               IFNULL(t.weekly_unlocked, 0) as daily_done,
               IFNULL(t.monthly_unlocked, 0) as weekly_done,
               IFNULL(t.monthly_completed, 0) as monthly_done
        FROM comments c
        JOIN users u ON c.user_id = u.id
        LEFT JOIN user_tasks t ON u.id = t.user_id
        WHERE c.post_id = ?
        ORDER BY c.created_at ASC
    """, (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'
# --- assignment #1
from datetime import datetime, timedelta
def init_task_table():
    """Creates the user_tasks table or updates it with new columns if it already exists."""
    with app.app_context():
        db = get_db()
        
        # 1. Create the table with all columns (for completely new setups)
        db.execute('''
            CREATE TABLE IF NOT EXISTS user_tasks (
                user_id INTEGER PRIMARY KEY,
                start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                weekly_unlocked BOOLEAN DEFAULT 0,
                monthly_unlocked BOOLEAN DEFAULT 0,
                monthly_completed BOOLEAN DEFAULT 0,
                daily_completed_at TIMESTAMP DEFAULT NULL,
                weekly_completed_at TIMESTAMP DEFAULT NULL
            )
        ''')

        # 2. Safely add the new columns for existing databases
        # (daily_completed_at / weekly_completed_at remember when a challenge was finished,
        #  so the next challenge only counts posts made after that moment)
        for column in ['monthly_completed BOOLEAN DEFAULT 0',
                       'daily_completed_at TIMESTAMP DEFAULT NULL',
                       'weekly_completed_at TIMESTAMP DEFAULT NULL']:
            try:
                db.execute(f'ALTER TABLE user_tasks ADD COLUMN {column}')
            except sqlite3.OperationalError:
                # If the column already exists, SQLite throws an error. We just ignore it and move on.
                pass

        db.commit()
    
def update_user_tasks(user_id):
    """Checks a user's daily/weekly/monthly challenges and saves any unlocks.
    Called when the user creates a post and when the Tasks page is opened."""
    db = get_db()

    task_data = query_db('SELECT * FROM user_tasks WHERE user_id = ?', (user_id,), one=True)
    if not task_data:
        db.execute('INSERT INTO user_tasks (user_id) VALUES (?)', (user_id,))
        db.commit()
        task_data = query_db('SELECT * FROM user_tasks WHERE user_id = ?', (user_id,), one=True)

    weekly_unlocked = task_data['weekly_unlocked']
    monthly_unlocked = task_data['monthly_unlocked']
    daily_completed_at = task_data['daily_completed_at']
    weekly_completed_at = task_data['weekly_completed_at']
    if isinstance(daily_completed_at, str):
        daily_completed_at = datetime.strptime(daily_completed_at[:19], '%Y-%m-%d %H:%M:%S')
    if isinstance(weekly_completed_at, str):
        weekly_completed_at = datetime.strptime(weekly_completed_at[:19], '%Y-%m-%d %H:%M:%S')

    # Unlocks saved before completion times existed are checked again under the new rule
    if weekly_unlocked and not daily_completed_at:
        weekly_unlocked = monthly_unlocked = 0
        db.execute('UPDATE user_tasks SET weekly_unlocked = 0, monthly_unlocked = 0, monthly_completed = 0 WHERE user_id = ?', (user_id,))
        db.commit()
    elif monthly_unlocked and not weekly_completed_at:
        monthly_unlocked = 0
        db.execute('UPDATE user_tasks SET monthly_unlocked = 0, monthly_completed = 0 WHERE user_id = ?', (user_id,))
        db.commit()

    windows = [False, False, False, False]
    weekly_windows = [False, False, False, False]
    monthly_windows = [False, False, False] # 3 months
    
    posts = query_db('SELECT created_at FROM posts WHERE user_id = ? ORDER BY created_at ASC', (user_id,))
    
    # --- DAILY TASKS LOGIC (48 hours) ---
    if weekly_unlocked:
        windows = [True, True, True, True]
    elif posts:
        streak_start = None
        highest_window_reached = -1
        
        for post in posts:
            post_time = post['created_at']
            if isinstance(post_time, str):
                post_time = datetime.strptime(post_time, '%Y-%m-%d %H:%M:%S')
            
            if streak_start is None:
                streak_start = post_time
                highest_window_reached = 0
                continue
            
            hours_since_start = (post_time - streak_start).total_seconds() / 3600.0
            window_index = int(hours_since_start // 48)
            
            if window_index == highest_window_reached:
                pass
            elif window_index == highest_window_reached + 1:
                highest_window_reached = window_index
                if highest_window_reached == 3:
                    break
            elif window_index > highest_window_reached + 1:
                streak_start = post_time
                highest_window_reached = 0
        
        if highest_window_reached < 3 and streak_start is not None:
            now = datetime.utcnow()
            hours_since_start_now = (now - streak_start).total_seconds() / 3600.0
            current_expected_window = int(hours_since_start_now // 48)
            if current_expected_window > highest_window_reached + 1:
                highest_window_reached = -1
        
        for i in range(4):
            if i <= highest_window_reached:
                windows[i] = True

        if all(windows):
            daily_completed_at = post_time  # the post that completed the last window
            db.execute('UPDATE user_tasks SET weekly_unlocked = 1, daily_completed_at = ? WHERE user_id = ?',
                       (daily_completed_at.strftime('%Y-%m-%d %H:%M:%S'), user_id))
            db.commit()
            weekly_unlocked = 1

    # --- WEEKLY TASKS LOGIC (168 hours) ---
    if monthly_unlocked:
        weekly_windows = [True, True, True, True]
    elif weekly_unlocked and posts:
        w_streak_start = None
        w_highest_window = -1
        
        for post in posts:
            post_time = post['created_at']
            if isinstance(post_time, str):
                post_time = datetime.strptime(post_time, '%Y-%m-%d %H:%M:%S')

            # Only posts made after the daily challenge was completed count
            if post_time <= daily_completed_at:
                continue
            
            if w_streak_start is None:
                w_streak_start = post_time
                w_highest_window = 0
                continue
            
            hours_since_start = (post_time - w_streak_start).total_seconds() / 3600.0
            window_index = int(hours_since_start // 168)
            
            if window_index == w_highest_window:
                pass
            elif window_index == w_highest_window + 1:
                w_highest_window = window_index
                if w_highest_window == 3:
                    break
            elif window_index > w_highest_window + 1:
                w_streak_start = post_time
                w_highest_window = 0
                
        if w_highest_window < 3 and w_streak_start is not None:
            now = datetime.utcnow()
            hours_now = (now - w_streak_start).total_seconds() / 3600.0
            current_w_window = int(hours_now // 168)
            if current_w_window > w_highest_window + 1:
                w_highest_window = -1
                
        for i in range(4):
            if i <= w_highest_window:
                weekly_windows[i] = True
                
        if all(weekly_windows):
            weekly_completed_at = post_time  # the post that completed the last window
            db.execute('UPDATE user_tasks SET monthly_unlocked = 1, weekly_completed_at = ? WHERE user_id = ?',
                       (weekly_completed_at.strftime('%Y-%m-%d %H:%M:%S'), user_id))
            db.commit()
            monthly_unlocked = 1

    # --- MONTHLY TASKS LOGIC (720 hours / 30 days) ---
    if monthly_unlocked and posts:
        m_streak_start = None
        m_highest_window = -1
        
        for post in posts:
            post_time = post['created_at']
            if isinstance(post_time, str):
                post_time = datetime.strptime(post_time, '%Y-%m-%d %H:%M:%S')

            # Only posts made after the weekly challenge was completed count
            if post_time <= weekly_completed_at:
                continue
            
            if m_streak_start is None:
                m_streak_start = post_time
                m_highest_window = 0
                continue
            
            hours_since_start = (post_time - m_streak_start).total_seconds() / 3600.0
            window_index = int(hours_since_start // 720) 
            
            if window_index == m_highest_window:
                pass
            elif window_index == m_highest_window + 1:
                m_highest_window = window_index
                if m_highest_window == 2: # 3 months total
                    break
            elif window_index > m_highest_window + 1:
                m_streak_start = post_time
                m_highest_window = 0
                
        if m_highest_window < 2 and m_streak_start is not None:
            now = datetime.utcnow()
            hours_now = (now - m_streak_start).total_seconds() / 3600.0
            current_m_window = int(hours_now // 720)
            if current_m_window > m_highest_window + 1:
                m_highest_window = -1
                
        for i in range(3):
            if i <= m_highest_window:
                monthly_windows[i] = True

        if all(monthly_windows):
            db.execute('UPDATE user_tasks SET monthly_completed = 1 WHERE user_id = ?', (user_id,))
            db.commit()

    return windows, weekly_windows, monthly_windows, weekly_unlocked, monthly_unlocked

@app.route('/tasks')
def tasks():
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to view tasks.', 'danger')
        return redirect(url_for('login'))

    windows, weekly_windows, monthly_windows, weekly_unlocked, monthly_unlocked = update_user_tasks(user_id)

    # --- AUTO-CHECK ALL USERS' TASKS BEFORE SHOWING STATS ---
    all_users = query_db('SELECT id FROM users')
    for u in all_users:
        update_user_tasks(u['id'])

    # --- COMMUNITY STATS & USER PERCENTAGES ---
    total_users_count = query_db('SELECT COUNT(id) as cnt FROM users', one=True)['cnt']
    
    # Counts
    daily_completed_count = query_db('SELECT COUNT(user_id) as cnt FROM user_tasks WHERE weekly_unlocked = 1', one=True)['cnt']
    weekly_completed_count = query_db('SELECT COUNT(user_id) as cnt FROM user_tasks WHERE monthly_unlocked = 1', one=True)['cnt']
    monthly_completed_count = query_db('SELECT COUNT(user_id) as cnt FROM user_tasks WHERE monthly_completed = 1', one=True)['cnt']
    
    # Calculate percentages (prevent division by zero if total_users is 0)
    daily_pct = round((daily_completed_count / total_users_count) * 100) if total_users_count > 0 else 0
    weekly_pct = round((weekly_completed_count / total_users_count) * 100) if total_users_count > 0 else 0
    monthly_pct = round((monthly_completed_count / total_users_count) * 100) if total_users_count > 0 else 0
    
    # Check current user's personal completion status
    current_user_task = query_db('SELECT * FROM user_tasks WHERE user_id = ?', (user_id,), one=True)
    user_has_daily = current_user_task['weekly_unlocked'] == 1 if current_user_task else False
    user_has_weekly = current_user_task['monthly_unlocked'] == 1 if current_user_task else False
    user_has_monthly = current_user_task['monthly_completed'] == 1 if current_user_task else False

    return render_template('tasks.html.j2', 
                           windows=windows, 
                           weekly_windows=weekly_windows,
                           monthly_windows=monthly_windows,
                           weekly_unlocked=weekly_unlocked, 
                           monthly_unlocked=monthly_unlocked,
                           daily_pct=daily_pct,
                           weekly_pct=weekly_pct,
                           monthly_pct=monthly_pct,
                           user_has_daily=user_has_daily,
                           user_has_weekly=user_has_weekly,
                           user_has_monthly=user_has_monthly)

# --- Assignment 2.3: Design Claim - Normative Statistics + Reputation Systems
# Shows how common good behaviour is (community pulse) and rewards each user's
# run of violation-free content (clean streak). Only good behaviour is put on
# display, since showing many violations makes misbehaviour look normal.

CLEAN_STREAK_BADGES = [  # (streak needed, icon, title), highest first
    (100, '🏆', 'Community Pillar'),
    (50, '🛡️', 'Guardian'),
    (10, '🕊️', 'Peacekeeper'),
]
PULSE_CACHE_SECONDS = 300
PULSE_MIN_CLEAN_PCT = 50  # below this the "norm" isn't the majority, so don't advertise it
_pulse_cache = {'time': 0, 'data': None}

def community_pulse():
    """Returns the share of all posts/comments with no violations and the number of
    members whose content is entirely clean. Cached, as it scores every item."""
    now = time.time()
    if _pulse_cache['data'] and now - _pulse_cache['time'] < PULSE_CACHE_SECONDS:
        return _pulse_cache['data']

    rows = query_db('''
        SELECT user_id, content FROM posts
        UNION ALL
        SELECT user_id, content FROM comments
    ''') or []

    clean_items = 0
    authors, authors_with_violations = set(), set()
    for row in rows:
        authors.add(row['user_id'])
        if moderate_content(row['content'])[1] == 0:
            clean_items += 1
        else:
            authors_with_violations.add(row['user_id'])

    total = len(rows)
    clean_pct = round(clean_items / total * 100) if total else 100
    data = {
        'clean_pct': clean_pct,
        'clean_members': len(authors - authors_with_violations),
        'show': clean_pct >= PULSE_MIN_CLEAN_PCT,
    }
    _pulse_cache.update(time=now, data=data)
    return data

def clean_streak(user_id):
    """Returns the user's current and best run of consecutive violation-free
    posts/comments, their current badge, and the next badge to aim for."""
    rows = query_db('''
        SELECT content, created_at FROM posts WHERE user_id = ?
        UNION ALL
        SELECT content, created_at FROM comments WHERE user_id = ?
        ORDER BY created_at ASC
    ''', (user_id, user_id)) or []

    current = best = 0
    for row in rows:
        if moderate_content(row['content'])[1] == 0:
            current += 1
            best = max(best, current)
        else:
            current = 0

    badge = next((b for b in CLEAN_STREAK_BADGES if current >= b[0]), None)
    next_badge = next((b for b in reversed(CLEAN_STREAK_BADGES) if current < b[0]), None)
    return {
        'current': current,
        'best': best,
        'badge': badge,
        'next_badge': next_badge,
        'progress_pct': round(current / next_badge[0] * 100) if next_badge else 100,
    }

@app.template_global()
def streak_badge(user_id):
    """Template helper: the clean streak badge shown next to a username.
    Cached per request so authors appearing several times are scored once."""
    cache = g.setdefault('streak_badges', {})
    if user_id not in cache:
        cache[user_id] = clean_streak(user_id)['badge']
    return cache[user_id]

# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if user is None:
        return 0.0

    # Step 1: profile score
    _, profile_score = moderate_content(user['profile'])

    # Step 2: average post score (0 if no posts)
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,)) or []
    post_scores = [moderate_content(p['content'])[1] for p in posts]
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0

    # Step 3: average comment score (0 if no comments)
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,)) or []
    comment_scores = [moderate_content(c['content'])[1] for c in comments]
    average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

    # Step 4: weighted combination
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: account age multiplier
    created_at = user['created_at']
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    account_age_days = (datetime.utcnow() - created_at).days if created_at else 0

    if account_age_days < 7:
        user_risk_score = content_risk_score * 1.5
    elif account_age_days < 30:
        user_risk_score = content_risk_score * 1.2
    else:
        user_risk_score = content_risk_score

    # Step 6: cap at 5.0
    return min(5.0, user_risk_score)


def _build_word_pattern(terms):
    """Builds a case-insensitive, whole-word/phrase regex from a list of terms.
    Longer terms come first so e.g. 'pissed off' wins over 'pissed'."""
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    # Lookarounds instead of \b so phrases starting/ending in non-word chars (e.g. '100% free') still work
    return re.compile(r'(?<!\w)(?:' + '|'.join(escaped) + r')(?!\w)', re.IGNORECASE)

TIER1_PATTERN = _build_word_pattern(TIER1_WORDS)
TIER2_PATTERN = _build_word_pattern(TIER2_PHRASES)
TIER3_PATTERN = _build_word_pattern(TIER3_WORDS)
URL_PATTERN = re.compile(
    r'\b(?:https?://|www\.)\S+'
    r'|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|edu|gov|info|biz|me|ly|xyz|ru|uk|de|fi|eu)\b(?:/\S*)?',
    re.IGNORECASE
)


# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    if not content:
        return content or '', 0.0

    # Stage 1.1: severe violations remove the whole content
    if TIER1_PATTERN.search(content):
        return "[content removed due to severe violation]", 5.0
    if TIER2_PATTERN.search(content):
        return "[content removed due to spam/scam policy]", 5.0

    # Stage 1.2: scored violations & filtering
    score = 0.0

    # Rule 1.2.1: mask Tier 3 words, +2.0 each
    moderated_content, tier3_count = TIER3_PATTERN.subn(lambda m: '*' * len(m.group(0)), content)
    score += 2.0 * tier3_count

    # Rule 1.2.2: remove links, +2.0 each
    moderated_content, url_count = URL_PATTERN.subn('[link removed]', moderated_content)
    score += 2.0 * url_count

    # Rule 1.2.3: excessive capitalization (content not modified)
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) > 15 and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
        score += 0.5

    return moderated_content, score

# Coding Assignment #3
# Common words that say nothing about a post's topic, ignored by recommend()
RECOMMEND_STOPWORDS = set('''
the and for are but not you all any can had her was one our out has have him his how its may new now
old see two way who did get got let put say she too use that with this from they will what when your
just like been were more some than them then there these into only also very about would could should
their which while where after before because really think know going want make made much many most
over such even here well back still good great love today time feel day people don't i'm it's can't
'''.split())

# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    # Content-based filtering (TF-IDF keyword profile of the posts the user reacted to
    # positively) plus a social boost for authors the user follows or already liked.
    POSITIVE_REACTIONS = ('like', 'love', 'laugh', 'haha', 'wow')
    FOLLOW_BOOST = 0.15
    LIKED_AUTHOR_BOOST = 0.05   # per earlier liked post by the same author
    MAX_LIKED_AUTHOR_BOOST = 0.15
    COUNT = 5

    posts = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
               IFNULL(t.weekly_unlocked, 0) as daily_done,
               IFNULL(t.monthly_unlocked, 0) as weekly_done,
               IFNULL(t.monthly_completed, 0) as monthly_done,
               (SELECT COUNT(*) FROM reactions r
                WHERE r.post_id = p.id AND r.reaction_type IN (?, ?, ?, ?, ?)) as positive_count
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN user_tasks t ON u.id = t.user_id
    ''', POSITIVE_REACTIONS) or []

    followed = set()
    reacted, liked = set(), set()
    if user_id:
        followed = {r['followed_id'] for r in query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}
        for r in query_db('SELECT post_id, reaction_type FROM reactions WHERE user_id = ?', (user_id,)):
            reacted.add(r['post_id'])
            if r['reaction_type'] in POSITIVE_REACTIONS:
                liked.add(r['post_id'])

    # Candidates: posts the user hasn't written or reacted to yet
    candidates = [p for p in posts if p['user_id'] != user_id and p['id'] not in reacted]
    if filter_following:
        candidates = [p for p in candidates if p['user_id'] in followed]

    liked_posts = [p for p in posts if p['id'] in liked]

    if liked_posts:
        # --- TF-IDF vectors for every post ---
        def tokenize(text):
            return [w for w in re.findall(r"[a-z']{3,}", text.lower()) if w not in RECOMMEND_STOPWORDS]

        tokens = {p['id']: tokenize(p['content']) for p in posts}
        doc_freq = collections.Counter(w for words in tokens.values() for w in set(words))
        total_docs = len(posts)

        def tfidf(words):
            counts = collections.Counter(words)
            return {w: c * math.log(total_docs / doc_freq[w]) for w, c in counts.items()}

        def cosine(a, b):
            dot = sum(v * b.get(w, 0) for w, v in a.items())
            norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
            return dot / norm if norm else 0.0

        # User profile = sum of the TF-IDF vectors of the liked posts
        profile = collections.Counter()
        for p in liked_posts:
            profile.update(tfidf(tokens[p['id']]))
        liked_authors = collections.Counter(p['user_id'] for p in liked_posts)

        def score(p):
            s = cosine(tfidf(tokens[p['id']]), profile)
            if p['user_id'] in followed:
                s += FOLLOW_BOOST
            s += min(MAX_LIKED_AUTHOR_BOOST, LIKED_AUTHOR_BOOST * liked_authors[p['user_id']])
            return s
    else:
        # Cold start (no positive reactions yet): most liked posts, followed authors first
        def score(p):
            return (p['user_id'] in followed, p['positive_count'])

    ranked = sorted(candidates, key=lambda p: (score(p), p['created_at']), reverse=True)

    recommended_posts = []
    for p in ranked:
        # Don't recommend posts that moderation would remove
        if moderate_content(p['content'])[1] >= 5:
            continue
        recommended_posts.append(p)
        if len(recommended_posts) == COUNT:
            break

    recommended_posts.sort(key=lambda p: p['created_at'], reverse=True)
    return recommended_posts

# --- Assignment 3.2: Design Claims
# DC20: newcomers' posts are assigned to well-behaved old-timers who are asked to welcome them
#       (which also gives newcomers the friendly early interactions of DC18).
# DC25: progressive access control - newcomers can post once a day in their first week,
#       until they have shown they follow the rules with a few clean posts. Comments are never limited.

NEWCOMER_DAYS = 7               # an account younger than this is a newcomer
WELCOMER_MIN_AGE_DAYS = 30      # welcomers must be old-timers...
WELCOMER_MAX_RISK = 1.0         # ...with no risk (user_risk_analysis)...
WELCOMERS_PER_POST = 2          # ...and a clean streak badge
NEWCOMER_POST_INTERVAL = timedelta(hours=24)
NEWCOMER_CLEAN_POSTS_TO_UNLOCK = 3

def init_newcomer_tables():
    """Creates the table that stores which old-timer should welcome which newcomer post."""
    with app.app_context():
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS welcome_assignments (
                post_id INTEGER NOT NULL,
                welcomer_id INTEGER NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (post_id, welcomer_id)
            )
        ''')
        db.commit()

def account_age_days(user_id):
    user = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user or not user['created_at']:
        return None
    created_at = user['created_at']
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
    return (datetime.utcnow() - created_at).total_seconds() / 86400

@app.template_global()
def is_newcomer(user_id):
    """Template helper for the "New member" tag. Cached per request."""
    cache = g.setdefault('newcomers', {})
    if user_id not in cache:
        age = account_age_days(user_id)
        cache[user_id] = age is not None and age < NEWCOMER_DAYS
    return cache[user_id]

def assign_welcomers(post_id, author_id):
    """DC20: gives a newcomer's post to randomly chosen well-behaved old-timers."""
    if not is_newcomer(author_id):
        return
    candidates = query_db('''
        SELECT id FROM users
        WHERE id != ? AND username != 'admin' AND created_at <= datetime('now', ?)
    ''', (author_id, f'-{WELCOMER_MIN_AGE_DAYS} days')) or []
    candidate_ids = [c['id'] for c in candidates]
    random.shuffle(candidate_ids)

    db = get_db()
    chosen = 0
    for candidate_id in candidate_ids:
        # Cheapest check first; stop as soon as we have enough welcomers
        if clean_streak(candidate_id)['badge'] and user_risk_analysis(candidate_id) < WELCOMER_MAX_RISK:
            db.execute('INSERT OR IGNORE INTO welcome_assignments (post_id, welcomer_id) VALUES (?, ?)',
                       (post_id, candidate_id))
            chosen += 1
            if chosen == WELCOMERS_PER_POST:
                break
    db.commit()

def welcome_queue(user_id):
    """Newcomer posts assigned to this user that nobody has replied to yet."""
    if not user_id:
        return []
    rows = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM welcome_assignments w
        JOIN posts p ON p.id = w.post_id
        JOIN users u ON u.id = p.user_id
        WHERE w.welcomer_id = ?
          AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id != p.user_id)
        ORDER BY p.created_at DESC
        LIMIT 5
    ''', (user_id,)) or []
    queue = []
    for row in rows:
        post = dict(row)
        post['content'], _ = moderate_content(post['content'])
        queue.append(post)
    return queue

@app.template_global()
def is_welcomer(user_id):
    return query_db('SELECT 1 FROM welcome_assignments WHERE welcomer_id = ? LIMIT 1', (user_id,), one=True) is not None

def newcomer_post_wait(user_id):
    """DC25: returns how long a newcomer must still wait before their next post, or None."""
    age = account_age_days(user_id)
    if age is None or age >= NEWCOMER_DAYS:
        return None
    posts = query_db('SELECT content, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user_id,)) or []
    clean_posts = sum(1 for p in posts if moderate_content(p['content'])[1] == 0)
    if not posts or clean_posts >= NEWCOMER_CLEAN_POSTS_TO_UNLOCK:
        return None
    last_post = posts[0]['created_at']
    if isinstance(last_post, str):
        last_post = datetime.strptime(last_post[:19], '%Y-%m-%d %H:%M:%S')
    wait = NEWCOMER_POST_INTERVAL - (datetime.utcnow() - last_post)
    return wait if wait.total_seconds() > 0 else None

def newcomer_status(user_id):
    """Everything the feed shows a newcomer: their posting limit and how many members welcomed them."""
    if not user_id or not is_newcomer(user_id):
        return None
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,)) or []
    clean_posts = sum(1 for p in posts if moderate_content(p['content'])[1] == 0)
    wait = newcomer_post_wait(user_id)
    welcomers = query_db('''
        SELECT COUNT(DISTINCT c.user_id) as cnt FROM comments c
        JOIN posts p ON p.id = c.post_id
        WHERE p.user_id = ? AND c.user_id != ?
    ''', (user_id, user_id), one=True)['cnt']
    return {
        'days_left': max(1, round(NEWCOMER_DAYS - account_age_days(user_id))),
        'limit_lifted': clean_posts >= NEWCOMER_CLEAN_POSTS_TO_UNLOCK,
        'clean_posts_needed': max(0, NEWCOMER_CLEAN_POSTS_TO_UNLOCK - clean_posts),
        'wait_hours': None if wait is None else max(1, round(wait.total_seconds() / 3600)),
        'welcomed_by': welcomers,
    }

# Runs on import too, so it also happens under `flask run` (Docker)
init_task_table()
init_newcomer_tables()

if __name__ == '__main__':
    app.run(debug=True, port=8080)

