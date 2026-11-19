#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

// Еталонна сума для заповнення arr[idx] = idx % 100
static int64_t expected_sum(int64_t n) {
    int64_t q = n / 100;
    int64_t r = n % 100;
    return q * 4950LL + (r * (r - 1)) / 2;
}

int main(int argc, char **argv) {
    MPI_Init(&argc, &argv);

    int rank, size;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);

    int64_t n = (argc > 1) ? atoll(argv[1]) : 1000000000LL;

    // Рівномірний розподіл індексів між процесами
    int64_t base = n / size;
    int64_t rem = n % size;
    int64_t start = rank * base + (rank < rem ? rank : rem);
    int64_t local_n = base + (rank < rem ? 1 : 0);

    // Виділяємо пам'ять лише під свою частину масиву (для -n >= 2 вміщується в ОЗП ВМ)
    int *local_arr = (int *)malloc((size_t)local_n * sizeof(int));
    if (local_arr != NULL) {
        for (int64_t i = 0; i < local_n; i++) {
            local_arr[i] = (int)((start + i) % 100);
        }
    }

    MPI_Barrier(MPI_COMM_WORLD);
    double t0 = MPI_Wtime();

    // 1. Обчислення локальної суми своєї частини масиву
    int64_t local_sum = 0;
    if (local_arr != NULL) {
        for (int64_t i = 0; i < local_n; i++) {
            local_sum += local_arr[i];
        }
    } else {
        // Резервний блоковий режим для -n 1 (якщо весь масив ~3.73 ГБ не вміщується в ОЗП одного вузла)
        const int64_t block_size = 16000000LL;
        int *buf = (int *)malloc((size_t)block_size * sizeof(int));
        if (!buf) {
            fprintf(stderr, "Memory allocation failed\n");
            MPI_Abort(MPI_COMM_WORLD, 1);
        }
        for (int64_t offset = 0; offset < local_n; offset += block_size) {
            int64_t len = (offset + block_size <= local_n) ? block_size : (local_n - offset);
            for (int64_t i = 0; i < len; i++) {
                buf[i] = (int)((start + offset + i) % 100);
            }
            for (int64_t i = 0; i < len; i++) {
                local_sum += buf[i];
            }
        }
        free(buf);
    }

    // 2. Синхронне об'єднання проміжних сум на процесі rank 0
    int64_t total_sum = 0;
    MPI_Reduce(&local_sum, &total_sum, 1, MPI_INT64_T, MPI_SUM, 0, MPI_COMM_WORLD);

    MPI_Barrier(MPI_COMM_WORLD);
    double t1 = MPI_Wtime();

    if (rank == 0) {
        int64_t want = expected_sum(n);
        printf("Elements (N): %lld | Processes: %d\n", (long long)n, size);
        printf("Result:       %lld (Expected: %lld, Match: %s)\n",
               (long long)total_sum, (long long)want, total_sum == want ? "OK" : "FAIL");
        printf("Time:         %.2f ms\n", (t1 - t0) * 1000.0);
    }

    free(local_arr);
    MPI_Finalize();
    return 0;
}