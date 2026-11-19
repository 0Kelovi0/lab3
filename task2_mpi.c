#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    TAG_TASK = 1,
    TAG_ACK = 2,
    TAG_READ_REQ = 3,
    TAG_READ_DATA = 4,
    TAG_RESULT = 5
};

typedef struct {
    int64_t lo;
    int64_t hi;
    int64_t n;
} Task;

typedef struct {
    int64_t g_lo;
    int64_t g_hi;
} RangeReq;

// Еталонна сума для заповнення a[i] = i % 10 + 1
static int64_t expected_sum(int64_t n) {
    int64_t q = n / 10;
    int64_t r = n % 10;
    return q * 55LL + (r * (r + 1)) / 2;
}

// Обробка вхідних запитів на читання правої половини від інших воркерів
static void serve_reads(const int64_t *local_data, int64_t block_size, int rank) {
    int flag = 0;
    MPI_Status st;
    int64_t owner_start = (int64_t)(rank - 1) * block_size;

    while (1) {
        MPI_Iprobe(MPI_ANY_SOURCE, TAG_READ_REQ, MPI_COMM_WORLD, &flag, &st);
        if (!flag) break;

        RangeReq req;
        MPI_Recv(&req, sizeof(RangeReq), MPI_BYTE, st.MPI_SOURCE, TAG_READ_REQ, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
        int64_t offset = req.g_lo - owner_start;
        int64_t count = req.g_hi - req.g_lo;
        MPI_Send(local_data + offset, (int)count, MPI_INT64_T, st.MPI_SOURCE, TAG_READ_DATA, MPI_COMM_WORLD);
    }
}

// Зчитування симетричного глобального діапазону [g_lo, g_hi)
static void read_range(int my_rank, const int64_t *local_data, int64_t block_size,
                       int64_t g_lo, int64_t g_hi, int64_t *buf) {
    int64_t cur = g_lo;
    int64_t buf_pos = 0;

    while (cur < g_hi) {
        int owner = (int)(cur / block_size) + 1;
        int64_t owner_start = (int64_t)(owner - 1) * block_size;
        int64_t offset = cur - owner_start;
        int64_t avail = block_size - offset;
        int64_t count = (g_hi - cur < avail) ? (g_hi - cur) : avail;

        if (owner == my_rank) {
            memcpy(buf + buf_pos, local_data + offset, (size_t)count * sizeof(int64_t));
        } else {
            RangeReq req = {cur, cur + count};
            MPI_Request recv_req;
            MPI_Irecv(buf + buf_pos, (int)count, MPI_INT64_T, owner, TAG_READ_DATA, MPI_COMM_WORLD, &recv_req);
            MPI_Send(&req, sizeof(RangeReq), MPI_BYTE, owner, TAG_READ_REQ, MPI_COMM_WORLD);

            int done = 0;
            while (!done) {
                MPI_Test(&recv_req, &done, MPI_STATUS_IGNORE);
                if (!done) {
                    serve_reads(local_data, block_size, my_rank);
                }
            }
        }

        cur += count;
        buf_pos += count;
    }
}

int main(int argc, char **argv) {
    MPI_Init(&argc, &argv);

    int rank, size;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);

    if (size < 2) {
        if (rank == 0) {
            fprintf(stderr, "Error: requires at least 2 MPI processes (1 controller + >=1 workers).\n");
        }
        MPI_Finalize();
        return 1;
    }

    int64_t n = (argc > 1) ? atoll(argv[1]) : 1000000000LL;
    int64_t max_chunk = (argc > 2) ? atoll(argv[2]) : (1LL << 21); // 2 097 152 елементів (~16 МБ)

    int workers = size - 1;
    int64_t block_size = (n + workers - 1) / workers;

    // Виділяємо пам'ять під свою частину масиву лише на робочих процесах (rank >= 1)
    int64_t *local_data = NULL;
    if (rank > 0) {
        int64_t g_start = (int64_t)(rank - 1) * block_size;
        int64_t g_end = g_start + block_size;
        if (g_start < n) {
            if (g_end > n) g_end = n;
            int64_t valid_len = g_end - g_start;
            local_data = (int64_t *)malloc((size_t)block_size * sizeof(int64_t));
            if (!local_data) {
                fprintf(stderr, "Rank %d: failed to allocate memory\n", rank);
                MPI_Abort(MPI_COMM_WORLD, 1);
            }
            for (int64_t i = 0; i < valid_len; i++) {
                local_data[i] = ((g_start + i) % 10) + 1;
            }
        }
    }

    MPI_Barrier(MPI_COMM_WORLD);
    double t0 = MPI_Wtime();

    if (rank == 0) {
        // ==================== КЕРУЮЧИЙ ПРОЦЕС (CONTROLLER) ====================
        int64_t cur_n = n;
        int wave = 0;

        int64_t *w_next = (int64_t *)malloc((size_t)(workers + 1) * sizeof(int64_t));
        int64_t *w_end = (int64_t *)malloc((size_t)(workers + 1) * sizeof(int64_t));

        while (cur_n > 1) {
            wave++;
            int64_t half = cur_n / 2;
            int active_tasks = 0;

            // Призначаємо кожному воркеру лише ті індекси [lo, hi), якими він володіє в ОЗП
            for (int w = 1; w <= workers; w++) {
                int64_t start = (int64_t)(w - 1) * block_size;
                int64_t end = start + block_size;
                if (end > half) end = half;

                w_next[w] = start;
                w_end[w] = end;

                if (start < end) {
                    int64_t hi = (start + max_chunk < end) ? (start + max_chunk) : end;
                    Task t = {start, hi, cur_n};
                    MPI_Send(&t, sizeof(Task), MPI_BYTE, w, TAG_TASK, MPI_COMM_WORLD);
                    w_next[w] = hi;
                    active_tasks++;
                }
            }

            // Видаємо наступні локальні чанки воркерам по мірі виконання
            while (active_tasks > 0) {
                MPI_Status status;
                int ack = 0;
                MPI_Recv(&ack, 1, MPI_INT, MPI_ANY_SOURCE, TAG_ACK, MPI_COMM_WORLD, &status);
                active_tasks--;

                int w = status.MPI_SOURCE;
                if (w_next[w] < w_end[w]) {
                    int64_t hi = (w_next[w] + max_chunk < w_end[w]) ? (w_next[w] + max_chunk) : w_end[w];
                    Task t = {w_next[w], hi, cur_n};
                    MPI_Send(&t, sizeof(Task), MPI_BYTE, w, TAG_TASK, MPI_COMM_WORLD);
                    w_next[w] = hi;
                    active_tasks++;
                }
            }

            cur_n -= half;
        }

        free(w_next);
        free(w_end);

        // Сигнал завершення роботи для всіх воркерів
        Task stop_task = {-1, -1, -1};
        for (int w = 1; w <= workers; w++) {
            MPI_Send(&stop_task, sizeof(Task), MPI_BYTE, w, TAG_TASK, MPI_COMM_WORLD);
        }

        // Отримуємо фінальний результат a[0] від першого воркера (rank 1)
        int64_t result = 0;
        MPI_Recv(&result, 1, MPI_INT64_T, 1, TAG_RESULT, MPI_COMM_WORLD, MPI_STATUS_IGNORE);

        double t1 = MPI_Wtime();
        int64_t want = expected_sum(n);

        printf("=== MPI Task 2: Wave Pairwise Reduction (Data Locality) ===\n");
        printf("Elements (N): %lld | Controller: 1 | Workers: %d | Waves: %d\n",
               (long long)n, workers, wave);
        printf("Result:       %lld (Expected: %lld, Match: %s)\n",
               (long long)result, (long long)want, result == want ? "OK" : "FAIL");
        printf("Time:         %.2f ms\n", (t1 - t0) * 1000.0);

    } else {
        // ==================== РОБОЧІ ПРОЦЕСИ (WORKERS) ====================
        int64_t *right_buf = (int64_t *)malloc((size_t)max_chunk * sizeof(int64_t));
        int64_t owner_start = (int64_t)(rank - 1) * block_size;

        while (1) {
            int has_task = 0;
            MPI_Status st;
            while (!has_task) {
                MPI_Iprobe(0, TAG_TASK, MPI_COMM_WORLD, &has_task, &st);
                if (!has_task) {
                    serve_reads(local_data, block_size, rank);
                }
            }

            Task t;
            MPI_Recv(&t, sizeof(Task), MPI_BYTE, 0, TAG_TASK, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
            if (t.lo < 0) {
                break;
            }

            int64_t len = t.hi - t.lo;
            int64_t r_lo = t.n - t.hi;
            int64_t r_hi = t.n - t.lo;

            // 1. Зчитуємо лише симетричну праву частину [n - hi, n - lo)
            read_range(rank, local_data, block_size, r_lo, r_hi, right_buf);

            // 2. Оновлюємо ліву частину прямо у власній локальній пам'яті: a[i] += a[n - 1 - i]
            int64_t local_offset = t.lo - owner_start;
            for (int64_t k = 0; k < len; k++) {
                local_data[local_offset + k] += right_buf[len - 1 - k];
            }

            int ack = 1;
            MPI_Send(&ack, 1, MPI_INT, 0, TAG_ACK, MPI_COMM_WORLD);
        }

        if (rank == 1) {
            MPI_Send(&local_data[0], 1, MPI_INT64_T, 0, TAG_RESULT, MPI_COMM_WORLD);
        }

        free(right_buf);
    }

    free(local_data);
    MPI_Finalize();
    return 0;
}