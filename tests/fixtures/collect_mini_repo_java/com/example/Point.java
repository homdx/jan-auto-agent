package com.example;

/**
 * A point in 2D space.
 * Immutable, like every Java record.
 */
public record Point(int x, int y) {
    public int sum() {
        return x + y;
    }
}
