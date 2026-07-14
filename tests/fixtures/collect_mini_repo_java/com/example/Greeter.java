package com.example;

import java.util.List;
import java.util.*;
import static java.lang.Math.max;

/**
 * Greets people, loudly.
 */
public class Greeter {
    private String prefix;

    public Greeter(String prefix) {
        this.prefix = prefix;
    }

    public String greet(String name) {
        return prefix + ", " + capitalize(name) + "!";
    }

    private String capitalize(String name) {
        return name.isEmpty() ? name : Character.toUpperCase(name.charAt(0)) + name.substring(1);
    }

    protected int longestOf(int a, int b) {
        return max(a, b);
    }
}

class InternalHelper {
    static int helperCount = 0;
}
