import os
import sys
import re
from contextlib import contextmanager
from functools import wraps
from getpass import getpass, getuser
from importlib import import_module
from posixpath import join

from fabric import Connection
from fabric import task
#from fabric.contrib.project import rsync_project
#from fabric.contrib.files import upload_template

from patchwork.files import exists#
from patchwork.transfers import rsync
from invocations.console import confirm#
from colorama import init
from colorama import Fore

init()

class Env:
    pass

env = Env()

################
# Config setup #
################

if not hasattr(env, "proj_app"):
    env.proj_app = "genin"
        
conf = {}
if sys.argv[0].split(os.sep)[-1] in ("fab", "fab-script.py"):
    # Ensure we import settings from the current dir
    try:
        conf = import_module(f"{env.proj_app}.settings").FABRIC
        try:
            conf["HOSTS"][0]
        except (KeyError, ValueError):
            raise ImportError
    except (ImportError, AttributeError):
        print("Aborting, no hosts defined.")
        exit()

env.db_pass = conf.get("DB_PASS", None)#
env.admin_user = conf.get("ADMIN_USER", "admin")
env.admin_pass = conf.get("ADMIN_PASS", None)
env.user = conf.get("SSH_USER", getuser())
env.hosts = conf.get("HOSTS", [""])

env.proj_name = conf.get("PROJECT_NAME", env.proj_app)
env.venv_home = f"/home/{env.user}/.virtualenvs"
env.venv_path = join(env.venv_home, env.proj_name)
env.proj_path = f"/home/{env.user}/django/{env.proj_name}"
env.manage = f"{env.venv_path}/bin/python {env.proj_path}/manage.py"
env.domains = conf.get("DOMAINS", [conf.get("LIVE_HOSTNAME", env.hosts[0])])
env.domains_nginx = " ".join(env.domains)
env.domains_regex = "|".join(env.domains)
env.domains_python = ", ".join(["'%s'" % s for s in env.domains])
#env.ssl_disabled = "#" if len(env.domains) > 1 else ""
env.ssl_disabled = "# "
env.locale = conf.get("LOCALE", "en_US.UTF-8")
env.vcs_tools = ["git", "hg"]
env.deploy_tool = conf.get("DEPLOY_TOOL", "rsync")
env.reqs_path = conf.get("REQUIREMENTS_PATH", None)
env.num_workers = conf.get("NUM_WORKERS",
                           "multiprocessing.cpu_count() * 2 + 1")
env.secret_key = conf.get("SECRET_KEY", "")
env.nevercache_key = conf.get("NEVERCACHE_KEY", "")

# if not env.secret_key and \
#         os.environ.get("DJANGO_SETTINGS_MODULE", "") != "docs_settings":
#     print("Aborting, no SECRET_KEY setting defined.")
#     exit()

# Remote git repos need to be "bare" and reside separated from the project
if env.deploy_tool == "git":
    env.repo_path = f"/home/{env.user}/git/{env.proj_name}.git"
else:
    env.repo_path = env.proj_path

def abort():
    exit()

def connect(c):
    if None == c:
        c = Connection(host=env.hosts[0], user=env.user)
    return c
    

# ##################
# # Template setup #
# ##################
# 
# Each template gets uploaded at deploy time, only if their
# contents has changed, in which case, the reload command is
# also run.

# templates = {
#     "nginx": {
#         "local_path": "deploy/nginx.conf.template",
#         "remote_path": "/etc/nginx/sites-enabled/%(proj_name)s.conf",
#         "reload_command": "nginx -t && service nginx restart",
#     },
#     "supervisor": {
#         "local_path": "deploy/supervisor.conf.template",
#         "remote_path": "/etc/supervisor/conf.d/%(proj_name)s.conf",
#         "reload_command": "supervisorctl update gunicorn_%(proj_name)s",
#     },
#     "cron": {
#         "local_path": "deploy/crontab.template",
#         "remote_path": "/etc/cron.d/%(proj_name)s",
#         "owner": "root",
#         "mode": "600",
#     },
#     "gunicorn": {
#         "local_path": "deploy/gunicorn.conf.py.template",
#         "remote_path": "%(proj_path)s/gunicorn.conf.py",
#     },
#     "settings": {
#         "local_path": "deploy/local_settings.py.template",
#         "remote_path": "%(proj_path)s/%(proj_app)s/local_settings.py",
#     },
# }
templates = {
    "nginx": {
        "local_path": "deploy/nginx.conf.template",
        "remote_path": f"/etc/nginx/sites-enabled/{env.proj_name}.conf",
        #"reload_command": "nginx -t && service nginx restart",
        "reload_command": "service nginx restart",
    },
    "supervisor": {
        "local_path": "deploy/supervisor.conf.template",
        "remote_path": f"/etc/supervisor/conf.d/{env.proj_name}.conf",
        "reload_command": f"supervisorctl update gunicorn_{env.proj_name}",
    },
    "cron": {
        "local_path": "deploy/crontab.template",
        "remote_path": f"/etc/cron.d/{env.proj_name}",
        "owner": "root",
        "mode": "600",
    },
    "gunicorn": {
        "local_path": "deploy/gunicorn.conf.py.template",
        "remote_path": f"{env.proj_path}/gunicorn.conf.py",
    },
    "settings": {
        "local_path": "deploy/local_settings.py.template",
        "remote_path": f"{env.proj_path}/{env.proj_app}/local_settings.py",
    },
}


######################################
# Context for virtualenv and project #
######################################

@contextmanager
def virtualenv(c):
    """
    Runs commands within the project's virtualenv.
    """
    with c.cd(env.venv_path):
        with c.prefix(f"source {env.venv_path}/bin/activate"):
            yield


@contextmanager
def project(c):
    """
    Runs commands within the project's directory.
    """
    with virtualenv(c):
        c.run("pwd")
        with c.cd(env.proj_path):
            yield
# 
# 
# @contextmanager
# def update_changed_requirements():
#     """
#     Checks for changes in the requirements file across an update,
#     and gets new requirements if changes have occurred.
#     """
#     reqs_path = join(env.proj_path, env.reqs_path)
#     get_reqs = lambda: run("cat %s" % reqs_path, show=False)
#     old_reqs = get_reqs() if env.reqs_path else ""
#     yield
#     if old_reqs:
#         new_reqs = get_reqs()
#         if old_reqs == new_reqs:
#             # Unpinned requirements should always be checked.
#             for req in new_reqs.split("\n"):
#                 if req.startswith("-e"):
#                     if "@" not in req:
#                         # Editable requirement without pinned commit.
#                         break
#                 elif req.strip() and not req.startswith("#"):
#                     if not set(">=<") & set(req):
#                         # PyPI requirement without version.
#                         break
#             else:
#                 # All requirements are pinned.
#                 return
#         pip("-r %s/%s" % (env.proj_path, env.reqs_path))
# 
# 
# ###########################################
# # Utils and wrappers for various commands #
# ###########################################
# 
def blue(text, bold=False):
    return Fore.BLUE + text + Fore.WHITE

def green(text, bold=False):
    return Fore.GREEN + text + Fore.WHITE

def red(text, bold=False):
    return Fore.RED + text + Fore.WHITE

def yellow(text, bold=False):
    return Fore.YELLOW + text + Fore.WHITE

def _print(output):
    print()
    print(output)
    print()


def print_command(command):
    _print(blue("$ ", bold=True) +
           yellow(command, bold=True) +
           red(" ->", bold=True))
@task
def run(c, command, show=True, *args, **kwargs):
    """
    Runs a shell comand on the remote server.
    """
    if show:
        print_command(command)
    return c.run(command, hide=(not show))


@task
def sudo(c, command, show=True, *args, **kwargs):
    """
    Runs a command as sudo on the remote server.
    """
    if show:
        print_command(command)
    return c.sudo(command, hide=(not show))


def log_call(func):
    @wraps(func)
    def logged(*args, **kawrgs):
        header = "-" * len(func.__name__)
        _print(green("\n".join([header, func.__name__, header]), bold=True))
        return func(*args, **kawrgs)
    return logged


# # def get_templates():
# #     """
# #     Returns each of the templates with env vars injected.
# #     """
# #     injected = {}
# #     for name, data in templates.items():
# #         injected[name] = dict([(k, v) for k, v in data.items()])
# #     return injected


def upload_template(c, local_path, remote_path, env, use_sudo=False, backup=False):
    #print(local_path, remote_path, env)
    remote_temp_path = os.path.join(env.proj_path, "temp")
    if not exists(c, remote_temp_path):
        run(c, f"mkdir -p {remote_temp_path}")
    #print(remote_temp_path)
    remote_temp_file = os.path.join(env.proj_path, "temp", remote_path.split("/")[-1])
    #print(remote_temp_file)
    #cmd = f"rsync -a  {local_path} {env.user}@{env.hosts[0]}:{remote_path}"
    cmd = f"rsync -a  {local_path} {env.user}@{env.hosts[0]}:{remote_temp_file}"
    print(cmd)
    os.system(cmd)
    
    sudo(c, f"mv {remote_temp_file} {remote_path}", show=True)
    if exists(c, remote_temp_path):
        run(c, f"rm -rf {remote_temp_path}")
    #exit()
    
def upload_template_and_reload(c, name):
    """
    Uploads a template only if it has changed, and if so, reload the
    related service.
    """
    #template = get_templates()[name]
    template = templates[name]
    local_path = template["local_path"]
    #print(local_path)
    if not os.path.exists(local_path):
        project_root = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(project_root, local_path)
    remote_path = template["remote_path"]
    reload_command = template.get("reload_command")
    owner = template.get("owner")
    mode = template.get("mode")
    remote_data = ""
    if exists(c, remote_path):
        remote_data = sudo(c, f"cat {remote_path}", show=False).stdout
        #print(remote_data)
        #with hide("stdout"):
            #remote_data = sudo("cat %s" % remote_path, show=False)
    
    with open(local_path, "r") as f:
        local_data = f.read()
        #print(local_data)
        # Escape all non-string-formatting-placeholder occurrences of '%':
        local_data = re.sub(r"%(?!\(\w+\)s)", "%%", local_data)
        if "%(db_pass)s" in local_data:
            env.db_pass = db_pass()
        
        for v in re.finditer(f"%\((\w+)\)s",local_data):
            #print(v.group(0), v.group(1))
            env_attr = v.group(1)
            if not hasattr(env, env_attr):
                raise Exception(f"env missing attribute '{env_attr}'")
#             else:
#                 print(getattr(env, env_attr))
            local_data = re.sub(f"%\({env_attr}\)s", getattr(env, env_attr), local_data)
        #local_data %= env
        #print(local_data)
    clean = lambda s: s.replace("\n", "").replace("\r", "").strip()
    if clean(remote_data) == clean(local_data):
        return
        
    temp_path = local_path + ".temp"
    f = open(temp_path, 'w')
    f.writelines(local_data)
    f.close()
    #upload_template(c, local_path, remote_path, env, use_sudo=True, backup=False)
    upload_template(c, temp_path, remote_path, env, use_sudo=True, backup=False)
    os.unlink(temp_path)
    if owner:
        sudo(c, f"chown {owner} {remote_path}")
    if mode:
        sudo(c, f"chmod {mode} {remote_path}")
    if reload_command:
        sudo(c, reload_command)


def rsync_upload(c):
    """
    Uploads the project with rsync excluding some files and folders.
    """
#     excludes = ["*.pyc", "*.pyo", "*.db", ".DS_Store", ".coverage",
#                 "local_settings.py", "/static", "/.git", "/.hg"]
    excludes = ["*.pyc", "*.pyo", "*.db", ".DS_Store", ".coverage",
                "local_settings.py", "/static", "/.git", "/.hg"]
    local_dir = os.getcwd() + os.sep
    print(c)
    print(local_dir)
    print(env.proj_path)
    cmd = f"rsync -a  --exclude \"{' '.join(excludes)}\" {local_dir} {env.user}@{env.hosts[0]}:{env.proj_path}"
    print(cmd)
    os.system(cmd)
    return
    return rsync(c,  source=local_dir, target=env.proj_path,
                         exclude=excludes)
# 
# 
# def vcs_upload():
#     """
#     Uploads the project with the selected VCS tool.
#     """
#     if env.deploy_tool == "git":
#         remote_path = "ssh://%s@%s%s" % (env.user, env.host_string,
#                                          env.repo_path)
#         if not exists(env.repo_path):
#             run("mkdir -p %s" % env.repo_path)
#             with cd(env.repo_path):
#                 run("git init --bare")
#         local("git push -f %s master" % remote_path)
#         with cd(env.repo_path):
#             run("GIT_WORK_TREE=%s git checkout -f master" % env.proj_path)
#             run("GIT_WORK_TREE=%s git reset --hard" % env.proj_path)
#     elif env.deploy_tool == "hg":
#         remote_path = "ssh://%s@%s/%s" % (env.user, env.host_string,
#                                           env.repo_path)
#         with cd(env.repo_path):
#             if not exists("%s/.hg" % env.repo_path):
#                 run("hg init")
#                 print(env.repo_path)
#             with fab_settings(warn_only=True):
#                 push = local("hg push -f %s" % remote_path)
#                 if push.return_code == 255:
#                     abort()
#             run("hg update")
# 
# 
def db_pass():
    """
    Prompts for the database password if unknown.
    """
    if not env.db_pass:
        env.db_pass = getpass("Enter the database password: ")
    return env.db_pass


@task
def apt(c, packages):
    """
    Installs one or more system packages via apt.
    """
    return sudo(c, "apt-get install -y -q " + packages)


@task
def pip(c, packages):
    """
    Installs one or more Python packages within the virtual environment.
    """
    with virtualenv(c):
        return c.run(f"pip install {packages}")


def postgres(c, command):
    """
    Runs the given command as the postgres user.
    """
    #print(command)
    hide = command.startswith("psql")
    return c.sudo(command, hide=hide, user="postgres")


@task
def psql(c, sql, show=True):
    """
    Runs SQL against the project's database.
    """
    out = postgres(c, f'psql -c "{sql}"')
    if show:
        print_command(sql)
    return out
# 
# 
@task
def backup(c, filename):
    """
    Backs up the project database.
    """
    tmp_file = f"/tmp/{filename}"
    print(tmp_file)
    # We dump to /tmp because user "postgres" can't write to other user folders
    # We cd to / because user "postgres" might not have read permissions
    # elsewhere.
    with c.cd("/"):
        run(c, "pwd")
        postgres(c, f"pg_dump -Fc {env.proj_name} > {tmp_file}")
#     run(c, "cp %s ." % tmp_file)
#     sudo(c, "rm -f %s" % tmp_file)


# @task
# def restore(filename):
#     """
#     Restores the project database from a previous backup.
#     """
#     return postgres("pg_restore -c -d %s %s" % (env.proj_name, filename))
# 
# 
@task
def python(c, code, show=True):
    """
    Runs Python code in the project's virtual environment, with Django loaded.
    """
    setup = "import os;" \
            f"os.environ[\'DJANGO_SETTINGS_MODULE\']=\'{env.proj_app}.settings\';" \
            "import django;" \
            "django.setup();"
    full_code = 'python -c "%s%s"' % (setup, code.replace("`", "\\\`"))
    print(full_code)
    with project(c):
        if show:
            print_command(code)
        result = run(c, full_code, show=False)
    return result
# 
# 
# def static():
#     """
#     Returns the live STATIC_ROOT directory.
#     """
#     return python("from django.conf import settings;"
#                   "print(settings.STATIC_ROOT)", show=False).split("\n")[-1]
# 
# 
@task
def manage(c, command):
    """
    Runs a Django management command.
    """
    return c.run(f"{env.manage} {command}")


# ###########################
# # Security best practices #
# ###########################
# 
# @task
# @log_call
# @hosts(["root@%s" % host for host in env.hosts])
# def secure(new_user=env.user):
#     """
#     Minimal security steps for brand new servers.
#     Installs system updates, creates new user (with sudo privileges) for future
#     usage, and disables root login via SSH.
#     """
#     run("apt-get update -q")
#     run("apt-get upgrade -y -q")
#     run("adduser --gecos '' %s" % new_user)
#     run("usermod -G sudo %s" % new_user)
#     run("sed -i 's:RootLogin yes:RootLogin no:' /etc/ssh/sshd_config")
#     run("service ssh restart")
#     print(green("Security steps completed. Log in to the server as '%s' from "
#                 "now on." % new_user, bold=True))
# 
# 
#########################
# Install and configure #
#########################

@task
#@log_call
def install(conn, c=None):
    """
    Installs the base system and Python requirements for the entire server.
    """
    c = connect(c)
    # Install system requirements
    sudo(c, "apt-get update -y -q")
    apt(c, "nginx libjpeg-dev python-dev python-setuptools git-core "
        "postgresql libpq-dev memcached supervisor python3-pip")
    apt(c, 'gcc rsync')
    run(c, f"mkdir -p /home/{env.user}/logs")

    # Install Python requirements
    sudo(c, "pip3 install pip")
    sudo(c, "pip3 install -U virtualenv virtualenvwrapper")

    # Set up virtualenv
    run(c, f"mkdir -p {env.venv_home}")
    run(c, f"echo 'export WORKON_HOME={env.venv_home}' >> /home/{env.user}/.bashrc")
    run(c, f"echo 'source /usr/local/bin/virtualenvwrapper.sh' >> /home/{env.user}/.bashrc")
    print(green("Successfully set up git, mercurial, pip, virtualenv, "
                "supervisor, memcached.", bold=True))

# @task
# #@log_call
# def xxx(c):
#     c = Connection(host=env.hosts[0], user=env.user)
#     with project(c):
#         python(c, 
#             "from django.conf import settings;"
#             "from django.contrib.sites.models import Site;"
#             f"Site.objects.filter(id=settings.SITE_ID).update(domain='{env.domains[0]}');")

@task
#@log_call
def create(conn, c=None):
    """
    Creates the environment needed to host the project.
    The environment consists of: system locales, virtualenv, database, project
    files, SSL certificate, and project-specific Python requirements.
    """
    c = connect(c)
    # Generate project locale
    locale = env.locale.replace("UTF-8", "utf8")
    if locale not in c.run("locale -a", hide=True).stdout:
        sudo(c, f"locale-gen {env.locale}")
        sudo(c, f"update-locale {env.locale}")
        sudo(c, "service postgresql restart")
        run(c, "exit")

    # Create project path
    run(c, f"mkdir -p {env.proj_path}")

    # Set up virtual env
    run(c, f"mkdir -p {env.venv_home}")
    with c.cd(env.venv_home):
        if exists(c, env.proj_name):
            if confirm(f"Virtualenv already exists in host server: {env.proj_name}"
                       "\nWould you like to replace it?"):
                c.run(f"rm -rf {env.proj_name}")
            else:
                abort()
        c.run(f"virtualenv -p python3 {env.proj_name}")

    # Upload project files
    if env.deploy_tool in env.vcs_tools:
        vcs_upload()
    else:
        rsync_upload(c)
        pass

    # Create DB and DB user
    pw = db_pass()
    #user_sql_args = (env.proj_name, pw.replace("'", "\'"))
    pw_replace = pw.replace("'", "\'")
    user_sql = f"CREATE USER {env.proj_name} WITH ENCRYPTED PASSWORD '{pw_replace}';"
    psql(c, user_sql, show=False)
    shadowed = "*" * len(pw)
    print_command(user_sql.replace(f"'{pw}'" , f"'{shadowed}'"))
    psql(c, f"CREATE DATABASE {env.proj_name} "
            f"WITH OWNER {env.proj_name} ENCODING = 'UTF8' "
            f"LC_CTYPE = '{env.locale}' LC_COLLATE = '{env.locale}' "
            f"TEMPLATE template0;")
# 
#     # Set up SSL certificate
#     if not env.ssl_disabled:
#         conf_path = "/etc/nginx/conf"
#         if not exists(conf_path):
#             sudo("mkdir %s" % conf_path)
#         with cd(conf_path):
#             crt_file = env.proj_name + ".crt"
#             key_file = env.proj_name + ".key"
#             if not exists(crt_file) and not exists(key_file):
#                 try:
#                     crt_local, = glob(join("deploy", "*.crt"))
#                     key_local, = glob(join("deploy", "*.key"))
#                 except ValueError:
#                     parts = (crt_file, key_file, env.domains[0])
#                     sudo("openssl req -new -x509 -nodes -out %s -keyout %s "
#                          "-subj '/CN=%s' -days 3650" % parts)
#                 else:
#                     upload_template(crt_local, crt_file, use_sudo=True)
#                     upload_template(key_local, key_file, use_sudo=True)
# 
    # Install project-specific requirements
    upload_template_and_reload(c, "settings")    
    with project(c):
        if env.reqs_path:
            pip(c, f"-r {env.proj_path}/{env.reqs_path}")
        pip(c, "gunicorn setproctitle psycopg2 "
            "django-compressor python-memcached")
    # Bootstrap the DB
        #manage(c, "createdb --noinput --nodata")
        manage(c, "migrate")
        python(c, 
            "from django.conf import settings;"
            "from django.contrib.sites.models import Site;"
            f"Site.objects.filter(id=settings.SITE_ID).update(domain='{env.domains[0]}');")
        for domain in env.domains:
            python(c, 
                "from django.contrib.sites.models import Site;"
                f"Site.objects.get_or_create(domain='{domain}');")
        if env.admin_pass:
            pw = env.admin_pass
            user = env.admin_user
            user_py = ("from django.contrib.auth import get_user_model;"
                       "User = get_user_model();"
                       f"u, _ = User.objects.get_or_create(username='{user}');"
                       "u.is_staff = u.is_superuser = True;"
                       f"u.set_password('{pw}');"
                       "u.save();")
            python(c, user_py, show=False)
            shadowed = "*" * len(pw)
            print_command(user_py.replace(f"'{pw}'", f"'{shadowed}'"))

    return True


@task
#@log_call
def remove(conn, c=None):
    """
    Blow away the current project.
    """
    c = connect(c)
    if exists(c, env.venv_path):
        run(c, f"rm -rf {env.venv_path}")
    if exists(c, env.proj_path):
        run(c, f"rm -rf {env.proj_path}")
    #for template in get_templates().values():
    for template in templates.values():
        remote_path = template["remote_path"]
        if exists(c, remote_path):
            sudo(c, f"rm {remote_path}")
    if exists(c, env.repo_path):
        run(c, "rm -rf {env.repo_path}")
    sudo(c, "supervisorctl update")
    psql(c, f"DROP DATABASE IF EXISTS {env.proj_name};")
    psql(c, f"DROP USER IF EXISTS {env.proj_name};")


##############
# Deployment #
##############

@task
#@log_call
def restart(conn, c=None):
    """
    Restart gunicorn worker processes for the project.
    If the processes are not running, they will be started.
    """
    c = connect(c)
    pid_path = f"{env.proj_path}/gunicorn.pid"
    if exists(c, pid_path):
        run(c, "kill -HUP `cat %s`" % pid_path)
    else:
        sudo(c, "supervisorctl update")

@task
#@log_call
def deploy(conn, c=None):
    """
    Deploy latest version of the project.
    Backup current version of the project, push latest version of the project
    via version control or rsync, install new requirements, sync and migrate
    the database, collect any new static assets, and restart gunicorn's worker
    processes for the project.
    """
    c = connect(c)
#    run(c, "uname -a")
#     print(c)
#     print(conn)
#     run(conn, "uname -a")
#     
#     conn.host=env.hosts[0]
#     conn.user=env.user
#     print(conn)
#     run(conn, "uname -a")
#     exit()
    if not exists(c, env.proj_path):
        if confirm("Project does not exist in host server: %s"
                   "\nWould you like to create it?" % env.proj_name):
            create()
        else:
            abort()

    # Backup current version of the project
#     with c.cd(env.proj_path):
#         backup(c, "last.db")
#     exit()
#     if env.deploy_tool in env.vcs_tools:
#         with cd(env.repo_path):
#             if env.deploy_tool == "git":
#                     run("git rev-parse HEAD > %s/last.commit" % env.proj_path)
#             elif env.deploy_tool == "hg":
#                     run("hg id -i > last.commit")
#         with project():
#             static_dir = static()
#             if exists(static_dir):
#                 run("tar -cf static.tar --exclude='*.thumbnails' %s" %
#                     static_dir)
#     else:
#         with cd(join(env.proj_path, "..")):
#             excludes = ["*.pyc", "*.pio", "*.thumbnails"]
#             exclude_arg = " ".join("--exclude='%s'" % e for e in excludes)
#             run("tar -cf {0}.tar {1} {0}".format(env.proj_name, exclude_arg))
# 
#     # Deploy latest version of the project
#     with update_changed_requirements():
#         if env.deploy_tool in env.vcs_tools:
#             vcs_upload()
#         else:
#             rsync_upload()
    with project(c):
        manage(c, "collectstatic -v 0 --noinput")
        manage(c, "migrate --noinput")
    for name in templates:
        upload_template_and_reload(c, name)
    restart(c)
    return True
# 
# 
# @task
# @log_call
# def rollback():
#     """
#     Reverts project state to the last deploy.
#     When a deploy is performed, the current state of the project is
#     backed up. This includes the project files, the database, and all static
#     files. Calling rollback will revert all of these to their state prior to
#     the last deploy.
#     """
#     with update_changed_requirements():
#         if env.deploy_tool in env.vcs_tools:
#             with cd(env.repo_path):
#                 if env.deploy_tool == "git":
#                         run("GIT_WORK_TREE={0} git checkout -f "
#                             "`cat {0}/last.commit`".format(env.proj_path))
#                 elif env.deploy_tool == "hg":
#                         run("hg update -C `cat last.commit`")
#             with project():
#                 with cd(join(static(), "..")):
#                     run("tar -xf %s/static.tar" % env.proj_path)
#         else:
#             with cd(env.proj_path.rsplit("/", 1)[0]):
#                 run("rm -rf %s" % env.proj_name)
#                 run("tar -xf %s.tar" % env.proj_name)
#     with cd(env.proj_path):
#         restore("last.db")
#     restart()
# 
# 
@task
#@log_call
def all(conn, c=None):
    """
    Installs everything required on a new system and deploy.
    From the base software, up to the deployed project.
    """
    c = connect(c)
    install(c)
    if create(c):
        deploy(c)
