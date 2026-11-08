#include <iostream>

// Some global variables to trigger GlobalOpt
int global_a = 42;
int global_b = 0;

// A function with dead code and constant folding opportunities
int compute(int x) {
    int y = x + 0;        // trivial addition
    int z = 100 * 1;      // multiplication by 1
    if (z > 50) {
        return y + global_a;  // involves a global variable
    }
    return y + global_b;
}

int main() {
    int val = compute(10);
    std::cout << val << std::endl;
    return 0;
}
