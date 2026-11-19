import subprocess
import argparse
import os

def compile_file(file_path):
    if not os.path.exists(file_path):
        print(f"Error: File '{file_path}' not found.")
        return
    subprocess.run(f"multipass transfer {file_path} manager:/home/ubuntu/cloud/", shell=True, check=True)
    full_file_name = os.path.basename(file_path)
    file_name, ext = os.path.splitext(full_file_name)
    compiler = "mpicxx -O3" if ext in [".cpp", ".cc", ".cxx"] else "mpicc -O3"
    print(f"File {full_file_name} transferred to manager.")
    subprocess.run(
        f"multipass exec manager -- {compiler} -o /home/ubuntu/cloud/{file_name} /home/ubuntu/cloud/{full_file_name}",
        shell=True, check=True
    )
    print("File compiled successfully.")

def run_mpi_program(file_name, num_threads):
    check = subprocess.run(f"multipass exec manager -- test -f /home/ubuntu/cloud/{file_name}", shell=True)
    if check.returncode != 0:
        print(f"Error: Compiled file '{file_name}' not found on manager machine.")
        return
    mpi_command = "mpiexec --hostfile /home/ubuntu/cloud/mpi_hosts --map-by node"
    if num_threads != -1:
        mpi_command += f" -n {num_threads}"
    subprocess.run(f"multipass exec manager -- {mpi_command} /home/ubuntu/cloud/{file_name}", shell=True)

def main():
    parser = argparse.ArgumentParser(description='Run MPI cluster')
    parser.add_argument('--c', '-c', metavar='path_to_file', type=str, help='Path to C/C++ file to compile')
    parser.add_argument('--r', '-r', metavar='file_name', type=str, help='Name of compiled binary to run')
    parser.add_argument('--n', '-n', type=int, default=-1, help='Number of MPI processes (optional)')
    args = parser.parse_args()

    if args.c:
        compile_file(args.c)
    elif args.r:
        run_mpi_program(args.r, args.n)
    else:
        print("Please provide either --c or --r option.")

if __name__ == "__main__":
    main()