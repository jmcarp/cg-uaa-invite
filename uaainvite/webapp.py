from email.mime.text import MIMEText
from email_validator import validate_email, EmailNotValidError
import codecs
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
import logging
import os
import smtplib
from uaainvite.clients import UAAClient, UAAError

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')

# key = connfiguration variable to be loaded from the environment
# value = The default to use if env var is not present
CONFIG_KEYS = {
    'UAA_BASE_URL': 'https://uaa.bosh-lite.com',
    'UAA_CLIENT_ID': None,
    'UAA_CLIENT_SECRET': None,
    'UAA_VERIFY_TLS': True,
    'SMTP_HOST': 'localhost',
    'SMTP_PORT': 25,
    'SMTP_FROM': 'no-reply@example.com',
    'SMTP_USER': None,
    'SMTP_PASS': None,
    'BRANDING_COMPANY_NAME': 'Cloud Foundry'
}


def generate_csrf_token():
    """Generate a secure string for use as a CSRF token"""
    if '_csrf_token' not in session:
        session['_csrf_token'] = codecs.encode(os.urandom(24), 'base-64').decode('utf-8').strip()

    return session['_csrf_token']


def str_to_bool(val):
    """Convert common yes/no true/false phrases to boolean

    Args:
        val(str): The string to convert

    Returns:
        True/False: The value of the string
        None: True/False value could not be determined

    """
    val = str(val).lower()

    if val in ['1', 'true', 'yes', 't', 'y']:
        return True
    if val in ['0', 'false', 'no', 'f', 'n', 'none', '']:
        return False

    return None


def send_email(app, email, subject, body):
    """Send an email via an external SMTP server

    Args:
        app(flask.App): The application sending the email
        email(str): The recepient of the message
        subject(str): The subject of the email
        body(str): The HTML body of the email

    Raises:
        socket.error: Could not connect to the SMTP Server

    Returns:
        True: The mail was accepted for delivery.

    """
    msg = MIMEText(body, 'html')
    msg['Subject'] = subject
    msg['To'] = email
    msg['From'] = app.config['SMTP_FROM']

    s = smtplib.SMTP(app.config['SMTP_HOST'], app.config['SMTP_PORT'])
    s.set_debuglevel(1)

    # if smtp credentials were provided, login
    if app.config['SMTP_USER'] is not None and app.config['SMTP_PASS'] is not None:
        s.login(app.config['SMTP_USER'], app.config['SMTP_PASS'])

    s.sendmail(app.config['SMTP_FROM'], [email], msg.as_string())
    s.quit()

    return True


def create_app(env=os.environ):
    """Create an instance of the web application"""
    # setup our app config
    app = Flask(__name__)
    app.secret_key = '\x08~m\xde\x87\xda\x17\x7f!\x97\xdf_@%\xf1{\xaa\xd8)\xcbU\xfe\x94\xc4'
    app.jinja_env.globals['csrf_token'] = generate_csrf_token

    # copy these environment variables into app.config

    for ck, default in CONFIG_KEYS.items():
        app.config[ck] = env.get(ck, default)

    # do boolean checks on this variable
    app.config['UAA_VERIFY_TLS'] = str_to_bool(app.config['UAA_VERIFY_TLS'])

    # make sure our base url doesn't have a trailing slash as UAA will flip out
    app.config['UAA_BASE_URL'] = app.config['UAA_BASE_URL'].rstrip('/')

    logging.info('Loaded application configuration:')
    for ck in sorted(CONFIG_KEYS.keys()):
        logging.info('{0}: {1}'.format(ck, app.config[ck]))

    @app.before_request
    def have_uaa_and_csrf_token():
        """Before each request, make sure we have a valid token from UAA.

        If we don't send them to UAA to start the oauth process.

        Technically we should bounce them through the renew token process if we already have one,
        but this app will be used sparingly, so it's fine to push them back through the authorize flow
        each time we need to renew our token.

        """
        # don't authenticate the oauth code receiver, or we'll never get the code back from UAA
        if request.endpoint and request.endpoint == 'oauth_login':
            return

        # check our token, and expirary date
        token = session.get('UAA_TOKEN', None)

        # if all looks good, setup the client
        if token:
            g.uaac = UAAClient(
                app.config['UAA_BASE_URL'],
                session['UAA_TOKEN'],
                verify_tls=app.config['UAA_VERIFY_TLS']
            )
        else:
            # if not forget the token, it's bad (if we have one)
            session.clear()

            return redirect('{0}/oauth/authorize?client_id={1}&response_type=code'.format(
                app.config['UAA_BASE_URL'],
                app.config['UAA_CLIENT_ID']
            ))

        # if it's a POST request, that's not to oauth_login
        # Then check for a CSRF token, if we don't have one, bail
        if request.method == "POST":
            csrf_token = session.pop('_csrf_token', None)
            if not csrf_token or csrf_token != request.form.get('_csrf_token'):
                logging.error('Error validating CSRF token.  Got: {0}; Expected: {1}'.format(
                    request.form.get('_csrf_token'),
                    csrf_token
                ))

                return render_template('error/csrf.html'), 400

    @app.route('/oauth/login')
    def oauth_login():
        """Called at the end of the oauth flow.  We'll receive an auth code from UAA and use it to
        retrieve a bearer token that we can use to actually do stuff
        """

        try:
            # remove any old tokens
            session.clear()

            # connect a client with no token
            uaac = UAAClient(app.config['UAA_BASE_URL'], None, verify_tls=app.config['UAA_VERIFY_TLS'])

            # auth with our client secret and the code they gave us
            token = uaac.oauth_token(request.args['code'], app.config['UAA_CLIENT_ID'], app.config['UAA_CLIENT_SECRET'])

            # if it's valid, but missing the scope we need, bail
            if 'scim.invite' not in token['scope'].split(' '):
                raise RuntimeError('Valid oauth autehntication but missing the scim.invite scope.  Scopes: {0}'.format(
                    token['scope']
                ))

            # make flask expire our session for us, by expiring it shortly before the token expires
            session.permanent = True
            app.permanent_session_lifetime = token['expires_in'] - 30

            # stash the stuff we care about
            session['UAA_TOKEN'] = token['access_token']
            session['UAA_TOKEN_SCOPES'] = token['scope'].split(' ')
            return redirect(url_for('index'))
        except UAAError:
            logging.exception('An invalid authorization_code was received from UAA')
            return render_template('error/token_validation.html'), 401
        except RuntimeError:
            logging.exception('Token validated but had wrong scope')
            return render_template('error/missing_scope.html'), 403

    @app.route('/', methods=['GET', 'POST'])
    def index():
        # start with giving them the form
        if request.method == 'GET':
            return render_template('index.html')

        # if we've reached here we are POST, and they've asked us to invite

        # validate the email address
        email = request.form.get('email', '')
        if not email:
            flash('Email cannot be blank.')
            return render_template('index.html')
        try:
            v = validate_email(email)  # validate and get info
            email = v["email"]  # replace with normalized form
        except EmailNotValidError as exc:
            # email is not valid, exception message is human-readable
            flash(str(exc))
            return render_template('index.html')

        # email is good, lets invite them
        try:
            invite = g.uaac.invite_users(email, app.config['UAA_BASE_URL'])

            if len(invite['failed_invites']):
                raise RuntimeError('UAA failed to invite the user.')

            invite = invite['new_invites'][0]

            branding = {
                'company_name': app.config['BRANDING_COMPANY_NAME']
            }

            # we invited them, send them the link to validate their account
            subject = render_template('email/subject.txt', invite=invite, branding=branding).strip()
            body = render_template('email/body.html', invite=invite, branding=branding)

            send_email(app, email, subject, body)
            return render_template('invite_sent.html')
        except Exception:
            logging.exception('An error occured during the invite process')
            return render_template('error/internal.html'), 500

    @app.route('/logout')
    def logout():
        session.clear()

        return redirect(url_for('index'))

    return app
