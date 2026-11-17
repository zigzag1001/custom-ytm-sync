import subprocess


def do_rsgain(output_root):
    command = ['rsgain', 'easy', '-p', 'no_album', '-S', output_root]
    subprocess.run(command, check=True)

